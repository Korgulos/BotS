"""Control-only email-code recovery for the managed EA session."""
import secrets
from datetime import datetime,timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin,urlparse,parse_qs
import httpx
from fastapi import Depends,HTTPException,Response
from pydantic import Field
from sqlalchemy import select
from . import db,ea_auth
from .models import StrictModel

EA_HOSTS={'accounts.ea.com','signin.ea.com'}
STORE='https://store.galaxy-of-heroes.starwars.ea.com/'
AUTH_URL='https://accounts.ea.com/connect/auth?mode=junoNff&client_id=SWGOH_SERVER_WEB_APP&response_type=code&hide_create=true&redirect_uri='+STORE
class Start(StrictModel):
    email:str=Field(min_length=5,max_length=254)
    game_closed:bool
class Finish(StrictModel):
    challenge_id:str=Field(min_length=20,max_length=100)
    pin:str=Field(pattern=r'^[0-9]{6}$')
    game_closed:bool
class Form(HTMLParser):
    def __init__(self,form_id):
        super().__init__();self.form_id=form_id;self.active=False;self.data={};self.names=set();self.action=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag=='form':
            self.active=a.get('id')==self.form_id
            if self.active:self.action=a.get('action','')
        if self.active and tag=='input' and a.get('name'):
            self.names.add(a['name'])
            if a.get('type')=='hidden':self.data[a['name']]=a.get('value','')
    def handle_endtag(self,tag):
        if tag=='form':self.active=False

def target(url,hosts=EA_HOSTS):
    p=urlparse(url)
    if p.scheme!='https' or p.hostname not in hosts or p.port not in (None,443) or p.username or p.password:raise ea_auth.AuthFailure('EA_REDIRECT_REJECTED')
    return url

def follow(client,response,callback=False):
    for _ in range(12):
        if not response.is_redirect:return response,None
        url=urljoin(str(response.url),response.headers['location']);p=urlparse(url)
        if callback and p.hostname=='store.galaxy-of-heroes.starwars.ea.com':
            target(url,{'store.galaxy-of-heroes.starwars.ea.com'})
            if p.path!='/':raise ea_auth.AuthFailure('EA_REDIRECT_REJECTED')
            return response,parse_qs(p.query)
        response=client.get(target(url))
    raise ea_auth.AuthFailure('EA_REDIRECT_LIMIT')

def html(response):
    if response.status_code!=200 or len(response.content)>2*1024*1024:raise ea_auth.AuthFailure('EA_FORM_UNAVAILABLE')
    return response.text

def cookies(client):
    return [{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path} for c in client.cookies.jar if c.domain in ea_auth.COOKIE_HOSTS]

def load_locked(c,account):
    seed=ea_auth.seed_config()
    if not seed or seed['account_id']!=account:raise HTTPException(409,'EA_AUTH_NOT_CONFIGURED')
    c.execute(select(db.accounts.c.id).where(db.accounts.c.id==account).with_for_update()).one()
    if not c.execute(select(db.sources.c.id).where(db.sources.c.id==seed['source_id'],db.sources.c.account_id==account)).first():raise HTTPException(409,'EA_SOURCE_UNVERIFIED')
    row=c.execute(select(db.auth_sessions).where(db.auth_sessions.c.account_id==account)).mappings().first()
    value=ea_auth.unseal(row['sealed']) if row and row['seed_id']==seed['seed_id'] else seed
    if value['account_id']!=account or value['source_id']!=seed['source_id']:raise HTTPException(409,'EA_ACCOUNT_MISMATCH')
    return value,row

def save(c,value,row,state,retry_after=None):
    values=dict(source_id=value['source_id'],seed_id=value['seed_id'],sealed=ea_auth.seal(value),state=state,updated_at=db.now(),retry_after=retry_after)
    if row:c.execute(db.auth_sessions.update().where(db.auth_sessions.c.account_id==value['account_id']).values(**values))
    else:c.execute(db.auth_sessions.insert().values(account_id=value['account_id'],**values))

def public(value,state):
    challenge=value.get('challenge',{});expires=challenge.get('expires_at')
    pending=bool(expires and not challenge.get('submitted') and datetime.fromisoformat(expires)>db.now())
    return dict(state=state,pin_pending=pending,challenge_id=challenge.get('id') if pending else None,
        pin_expires_at=expires if pending else None,session_expires_at=value.get('session',{}).get('_expiresAt'))

def start(engine,account,data,client_factory=httpx.Client):
    if not data.game_closed:raise HTTPException(409,'EA_CLOSE_GAME')
    email=data.email.strip()
    if email.count('@')!=1 or any(ch.isspace() for ch in email) or email.startswith('@') or email.endswith('@'):raise HTTPException(422,'EA_EMAIL_INVALID')
    error=None;result=None
    with engine.begin() as c:
        value,row=load_locked(c,account);now=db.now()
        last=value.get('login_requested_at')
        if last and datetime.fromisoformat(last)>now-timedelta(seconds=60):raise HTTPException(429,'EA_CODE_RATE_LIMIT')
        if ea_auth.busy(c,account):raise HTTPException(409,'EA_GAME_OR_TRANSACTION_ACTIVE')
        # Reserve and persist cooldown even when the upstream request fails.
        value['login_requested_at']=now.isoformat();value.pop('challenge',None)
        try:
            with client_factory(timeout=25,trust_env=False,follow_redirects=False) as client:
                response,_=follow(client,client.get(AUTH_URL));form=Form('login-form');form.feed(html(response))
                if form.action is None or 'email' not in form.names or form.data.get('loginMethod')!='emailOtc':raise ea_auth.AuthFailure('EA_INTERACTIVE_LOGIN_REQUIRED')
                url=target(urljoin(str(response.url),form.action))
                response,_=follow(client,client.post(url,data=dict(form.data,email=email,rememberMe='on')))
                verify=Form('verify-code-form');verify.feed(html(response))
                if verify.action is None or 'oneTimeCode' not in verify.names:raise ea_auth.AuthFailure('EA_CODE_NOT_SENT')
                value['challenge']=dict(id=secrets.token_urlsafe(24),url=target(urljoin(str(response.url),verify.action)),
                    fields=verify.data,cookies=cookies(client),expires_at=(now+timedelta(minutes=10)).isoformat())
        except ea_auth.AuthFailure as e:error=str(e)
        except Exception:error='EA_CODE_REQUEST_FAILED'
        state=error or 'pin_required';save(c,value,row,state);result=public(value,state)
    if error:raise HTTPException(409,error)
    return result

def finish(engine,account,data,client_factory=httpx.Client):
    if not data.game_closed:raise HTTPException(409,'EA_CLOSE_GAME')
    error=None;result=None
    # Commit a durable reservation before sending the one-time code. A process
    # restart after an uncertain upstream POST cannot replay that PIN.
    with engine.begin() as c:
        value,row=load_locked(c,account);challenge=value.get('challenge')
        if not challenge or not secrets.compare_digest(challenge['id'],data.challenge_id):raise HTTPException(409,'EA_PIN_CHALLENGE_MISSING')
        if challenge.get('submitted'):raise HTTPException(409,'EA_PIN_ALREADY_SUBMITTED')
        if datetime.fromisoformat(challenge['expires_at'])<=db.now():raise HTTPException(409,'EA_PIN_EXPIRED')
        if ea_auth.busy(c,account):raise HTTPException(409,'EA_GAME_OR_TRANSACTION_ACTIVE')
        challenge['submitted']=True
        save(c,value,row,'pin_verifying')
    with engine.begin() as c:
        value,row=load_locked(c,account);challenge=value.get('challenge')
        if not challenge or not secrets.compare_digest(challenge['id'],data.challenge_id):raise HTTPException(409,'EA_PIN_CHALLENGE_MISSING')
        if datetime.fromisoformat(challenge['expires_at'])<=db.now():raise HTTPException(409,'EA_PIN_EXPIRED')
        if ea_auth.busy(c,account):raise HTTPException(409,'EA_GAME_OR_TRANSACTION_ACTIVE')
        # One submission only. Persist removal on every outcome; an uncertain POST
        # is never resent, and an existing verified session survives failed recovery.
        value.pop('challenge',None)
        try:
            with client_factory(timeout=25,trust_env=False,follow_redirects=False) as client:
                for cookie in challenge['cookies']:
                    if cookie['domain'] in ea_auth.COOKIE_HOSTS:client.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie['path'])
                response,grant=follow(client,client.post(target(challenge['url']),data=dict(challenge['fields'],oneTimeCode=data.pin)),callback=True)
                if not grant or len(grant.get('code',[]))!=1:raise ea_auth.AuthFailure('EA_PIN_INVALID_OR_CHALLENGE')
                # The authorization code need not be consumed at the store. Successful
                # EA authentication already issued the remembered cookies used below.
                candidate={**value,'cookies':cookies(client)};candidate.pop('session',None)
                ea_auth.renew(candidate,client) # Checks expected ally code before saving.
                value=candidate
        except ea_auth.AuthFailure as e:error=str(e)
        except Exception:error='EA_PIN_CONFIRM_FAILED'
        state=error or 'ready';save(c,value,row,state,retry_after=db.now()+timedelta(minutes=1) if error else None);result=public(value,state)
    if error:raise HTTPException(409,error)
    return result

def install(app,engine,controller):
    @app.get('/v1/ea-login')
    def login_status(response:Response,who=Depends(controller)):
        response.headers["Cache-Control"]="no-store"
        with engine.begin() as c:
            value,row=load_locked(c,who['account_id'])
            return public(value,row['state'] if row else 'not_initialized')
    @app.post('/v1/ea-login/start')
    def request_pin(data:Start,response:Response,who=Depends(controller)):
        response.headers["Cache-Control"]="no-store"
        return start(engine,who['account_id'],data)
    @app.post('/v1/ea-login/finish')
    def confirm_pin(data:Finish,response:Response,who=Depends(controller)):
        response.headers["Cache-Control"]="no-store"
        return finish(engine,who['account_id'],data)
