"""Account-bound EA silent login. Credentials exist only in an encrypted private table.
Renewal is demand driven; this module never creates/binds players or retries RPCs.
"""
import base64,json,os,zlib
from datetime import datetime,timedelta
import httpx
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from . import db
from .direct_wire import field
from .direct_probe import RPC_URL
from .session_wire import build_request,fields,initial_projection,LIMIT

SEED='SWGOH_EA_LOGIN_SEED';KEY='SWGOH_AUTH_ENCRYPTION_KEY'
COOKIE_HOSTS={'accounts.ea.com','signin.ea.com','ea.com','.ea.com','.accounts.ea.com','.signin.ea.com'}
class AuthFailure(Exception):pass

def cipher():return Fernet(os.environ[KEY].encode())
def seal(value):return cipher().encrypt(json.dumps(value,separators=(',',':')).encode()).decode()
def unseal(value):return json.loads(cipher().decrypt(value.encode()))
def seed_config():
    if not os.environ.get(SEED):return None
    s=unseal(os.environ[SEED])
    if not all(s.get(k) for k in ('seed_id','account_id','source_id','expected_ally_code','cookies','template','auth_body')):raise ValueError('Invalid EA configuration')
    return s

def rpc(client,packet):
    with client.stream('POST',RPC_URL,content=packet,headers={'Content-Type':'application/x-protobuf','Accept':'application/x-protobuf'}) as r:
        if r.status_code!=200:raise AuthFailure('GAME_HTTP_REJECTED')
        data=bytearray()
        for chunk in r.iter_bytes():
            data.extend(chunk)
            if len(data)>LIMIT:raise AuthFailure('GAME_RESPONSE_LIMIT')
    root=fields(bytes(data))
    def one(tag,default):
        values=root.get(tag,[])
        if len(values)>1:raise AuthFailure('GAME_RESPONSE_INVALID')
        return values[0] if values else default
    code=one(5,0);body=one(4,b'');encoding=one(7,0);token=one(13,b'')
    if type(code) is not int or not isinstance(body,bytes) or not isinstance(token,bytes) or len(token)>32768:raise AuthFailure('GAME_RESPONSE_INVALID')
    if encoding==1:
        d=zlib.decompressobj(31);body=d.decompress(body,LIMIT+1)
        if len(body)>LIMIT or d.unconsumed_tail or not d.eof:raise AuthFailure('GAME_RESPONSE_LIMIT')
    elif encoding!=0:raise AuthFailure('GAME_RESPONSE_ENCODING')
    if code!=1:raise AuthFailure('GAME_RPC_'+str(code))
    return body,token.decode('utf8')

def renew(value,client):
    # Cookie scope is fixed to EA; neither webstore nor arbitrary redirect hosts receive it.
    for cookie in value['cookies']:
        if cookie['domain'] not in COOKIE_HOSTS:continue
        client.cookies.set(cookie['name'],cookie['value'],domain=cookie['domain'],path=cookie.get('path','/'))
    response=client.get('https://accounts.ea.com/connect/auth',params={
        'client_id':'ORIGIN_JS_SDK','response_type':'token','redirect_uri':'nucleus:rest','prompt':'none','token_format':'JWS'})
    # Save rotation even when EA requires a new interactive login. Never follow a challenge.
    value['cookies']=[{'name':c.name,'value':c.value,'domain':c.domain,'path':c.path} for c in client.cookies.jar if c.domain in COOKIE_HOSTS]
    if response.status_code!=200:raise AuthFailure('EA_LOGIN_REQUIRED')
    grant=response.json();token=grant.get('access_token','')
    if not isinstance(token,str) or len(token)>32768 or len(token.split('.'))!=3:raise AuthFailure('EA_TOKEN_INVALID')
    lifetime=int(grant.get('expires_in',0))
    if lifetime<600:raise AuthFailure('EA_TOKEN_EXPIRING')
    now=db.now();s=dict(value['template'])
    # The template contains only observed client settings; discard all old credentials.
    for name in ('authId','authToken','requestToken','hasNextRequestToken','requestTokenMode','_expiresAt'):s.pop(name,None)
    s.update(capturedAt=now.isoformat(),clientTime=int(now.timestamp()*1000),authId='',authToken='')
    payload=fields(base64.b64decode(value['auth_body'],validate=True));payload[1]=[token.encode()]
    payload[3]=[1] # DONT_BIND, regardless of the observed default.
    payload[10]=[0] # Never create a player if an EA account is not linked.
    envelope=fields(build_request(s,'Ping'))
    for tag in (7,8,36):envelope.pop(tag,None)
    envelope[4]=[b'AuthRpc'];envelope[5]=[b'DoAuthNucleus']
    envelope[6]=[b''.join(field(t,v) for t,vs in payload.items() for v in vs)]
    body,next_token=rpc(client,b''.join(field(t,v) for t,vs in envelope.items() for v in vs));auth=fields(body)
    if len(auth.get(1,[]))!=1 or len(auth.get(2,[]))!=1 or auth.get(3,[0])[0]:raise AuthFailure('GAME_AUTH_INVALID')
    s.update(authId=auth[1][0].decode(),authToken=auth[2][0].decode(),requestToken=next_token,
        requestTokenMode='next' if next_token else 'absent',hasNextRequestToken=bool(next_token))
    # Login's bootstrap token is consumed by this initial read. Later operations must
    # have verified absence of sequencing, exactly as observed in the PC client.
    body,next_token=rpc(client,build_request(s,'GetInitialData'))
    if str(initial_projection(body)['player']['allyCode'])!=str(value['expected_ally_code']):raise AuthFailure('ACCOUNT_MISMATCH')
    if next_token:raise AuthFailure('GAME_SEQUENCE_UNSUPPORTED')
    s.update(requestToken='',requestTokenMode='absent',hasNextRequestToken=False)
    _,next_token=rpc(client,build_request(s,'Ping'))
    if next_token:raise AuthFailure('GAME_SEQUENCE_UNSUPPORTED')
    s.update(_accountId=value['account_id'],_sourceId=value['source_id'],expectedAllyCode=value['expected_ally_code'],
        _expiresAt=(now+timedelta(seconds=min(lifetime-300,10800))).isoformat(),source='server-EA-silent-login')
    value['session']=s
    return s

def busy(c,account):
    now=db.now()
    online=c.execute(select(db.agents.c.source_id).join(db.sources,db.sources.c.id==db.agents.c.source_id).where(
        db.sources.c.account_id==account,db.agents.c.online.is_(True),db.agents.c.last_seen>now-timedelta(seconds=60))).first()
    active=c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==account,
        db.purchases.c.state.in_(['sending','checking_commit','preparing','queued_commit']))).first()
    return bool(online or active)

def session(engine,account,force=False,client_factory=httpx.Client):
    try:seed=seed_config()
    except Exception:raise HTTPException(409,'EA_AUTH_CONFIGURATION_INVALID')
    if not seed or seed['account_id']!=account:return None
    error=None;result=None
    with engine.begin() as c:
        # Share the account lock with transaction reservations; concurrent renewal
        # callers wait and use the already saved session instead of logging in twice.
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==account).with_for_update()).one()
        if not c.execute(select(db.sources.c.id).where(db.sources.c.id==seed['source_id'],db.sources.c.account_id==account)).first():raise HTTPException(409,'EA_SOURCE_UNVERIFIED')
        row=c.execute(select(db.auth_sessions).where(db.auth_sessions.c.account_id==account)).mappings().first()
        value=unseal(row['sealed']) if row and row['seed_id']==seed['seed_id'] else seed
        if value['account_id']!=account or value['source_id']!=seed['source_id']:raise HTTPException(409,'EA_ACCOUNT_MISMATCH')
        cached=value.get('session');now=db.now()
        if cached and not force and datetime.fromisoformat(cached['_expiresAt'])>now+timedelta(minutes=5):return cached
        if force and cached and row and row['updated_at']>now-timedelta(seconds=60):return cached
        if row and row['retry_after'] and row['retry_after']>now:raise HTTPException(409,'EA_AUTH_RETRY_LATER')
        if busy(c,account):raise HTTPException(409,'EA_GAME_OR_TRANSACTION_ACTIVE')
        try:
            with client_factory(timeout=25,trust_env=False,follow_redirects=False) as client:result=renew(value,client)
        except AuthFailure as e:error=str(e) # Only fixed application codes, never upstream text.
        except Exception:error='EA_AUTH_FAILED'
        if error:value.pop('session',None)
        values=dict(source_id=seed['source_id'],seed_id=seed['seed_id'],sealed=seal(value),
            state=error or 'ready',updated_at=db.now(),retry_after=db.now()+timedelta(minutes=10) if error else None)
        if row:c.execute(db.auth_sessions.update().where(db.auth_sessions.c.account_id==account).values(**values))
        else:c.execute(db.auth_sessions.insert().values(account_id=account,**values))
    if error:raise HTTPException(409,error)
    return result

def invalidate(engine,account):
    with engine.begin() as c:
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==account).with_for_update()).one()
        row=c.execute(select(db.auth_sessions).where(db.auth_sessions.c.account_id==account)).mappings().first()
        if row:
            value=unseal(row['sealed']);value.pop('session',None)
            c.execute(db.auth_sessions.update().where(db.auth_sessions.c.account_id==account).values(sealed=seal(value),state='game_session_rejected',updated_at=db.now()))

def status(engine,account):
    with engine.connect() as c:row=c.execute(select(db.auth_sessions).where(db.auth_sessions.c.account_id==account)).mappings().first()
    if not row:return {'state':'not_initialized','automatic_renewal':bool(os.environ.get(SEED))}
    value=unseal(row['sealed']);s=value.get('session',{})
    return {'state':row['state'],'automatic_renewal':True,'updated_at':row['updated_at'],
        'expires_at':s.get('_expiresAt'),'retry_after':row['retry_after']}
