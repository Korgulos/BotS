from datetime import timedelta
import httpx,pytest
from fastapi import HTTPException
from sqlalchemy import select
from test_ea_auth import configured
from swgoh_cloud import db,ea_auth,ea_login

LOGIN='<form id="login-form"><input type="hidden" name="_eventId" value="submit"><input type="hidden" name="loginMethod" value="emailOtc"><input name="email"></form>'
VERIFY='<form id="verify-code-form"><input name="oneTimeCode" type="hidden"><input name="_eventId" type="hidden" value="submit"></form>'

def factory(handler):return lambda **kwargs:httpx.Client(transport=httpx.MockTransport(handler),**kwargs)
def request_handler(request):
    if request.method=='GET':return httpx.Response(200,text=LOGIN)
    assert b'email=user%40example.com' in request.content
    return httpx.Response(200,text=VERIFY,headers={'set-cookie':'sid=private-challenge; Domain=.ea.com; Secure; Path=/'})

def test_durable_challenge_recovery_and_success(engine,monkeypatch):
    client,a,_,_=configured(engine,monkeypatch)
    d=ea_login.start(engine,a['account_id'],ea_login.Start(email='user@example.com',game_closed=True),factory(request_handler))
    assert d['pin_pending'] and d['challenge_id'] and 'private-challenge' not in str(d)
    def verify(request):
        assert request.url.host=='accounts.ea.com' and b'oneTimeCode=123456' in request.content
        return httpx.Response(302,headers={'location':ea_login.STORE+'?code=private-grant','set-cookie':'remid=renewed-cookie; Domain=.ea.com; Secure; Path=/'})
    def renew(value,client):
        assert any(c['name']=='remid' and c['value']=='renewed-cookie' for c in value['cookies'])
        value['session']={'_expiresAt':(db.now()+timedelta(hours=3)).isoformat()};return value['session']
    monkeypatch.setattr(ea_auth,'renew',renew)
    result=ea_login.finish(engine,a['account_id'],ea_login.Finish(challenge_id=d['challenge_id'],pin='123456',game_closed=True),factory(verify))
    assert result['state']=='ready' and not result['pin_pending']
    with engine.connect() as c:row=c.execute(select(db.auth_sessions)).mappings().one()
    assert '123456' not in row['sealed'] and 'renewed-cookie' not in row['sealed']
    with pytest.raises(HTTPException):ea_login.finish(engine,a['account_id'],ea_login.Finish(challenge_id=d['challenge_id'],pin='123456',game_closed=True),factory(lambda r:pytest.fail('Replayed PIN')))

def test_wrong_account_preserves_old_session_and_cookie(engine,monkeypatch):
    _,a,_,_=configured(engine,monkeypatch)
    d=ea_login.start(engine,a['account_id'],ea_login.Start(email='user@example.com',game_closed=True),factory(request_handler))
    with engine.begin() as c:
        v,row=ea_login.load_locked(c,a['account_id']);v['session']={'authToken':'existing-secret'};ea_login.save(c,v,row,'pin_required')
    def renew(*args):raise ea_auth.AuthFailure('ACCOUNT_MISMATCH')
    monkeypatch.setattr(ea_auth,'renew',renew)
    def handler(request):return httpx.Response(302,headers={'location':ea_login.STORE+'?code=grant'})
    with pytest.raises(HTTPException) as e:ea_login.finish(engine,a['account_id'],ea_login.Finish(challenge_id=d['challenge_id'],pin='123456',game_closed=True),factory(handler))
    assert e.value.detail=='ACCOUNT_MISMATCH'
    with engine.begin() as c:v,_=ea_login.load_locked(c,a['account_id'])
    assert v['session']['authToken']=='existing-secret' and v['cookies'][0]['value']=='private-cookie' and 'challenge' not in v

def test_start_cooldown_and_redirect_guard(engine,monkeypatch):
    _,a,_,_=configured(engine,monkeypatch)
    def bad(request):return httpx.Response(302,headers={'location':'https://untrusted.example/steal'})
    with pytest.raises(HTTPException) as e:ea_login.start(engine,a['account_id'],ea_login.Start(email='user@example.com',game_closed=True),factory(bad))
    assert e.value.detail=='EA_REDIRECT_REJECTED'
    with pytest.raises(HTTPException) as e:ea_login.start(engine,a['account_id'],ea_login.Start(email='user@example.com',game_closed=True),factory(lambda r:pytest.fail('Cooldown ignored')))
    assert e.value.status_code==429

def test_uncertain_pin_not_replayed_and_pending_blocks_renewal(engine,monkeypatch):
    _,a,_,_=configured(engine,monkeypatch)
    d=ea_login.start(engine,a['account_id'],ea_login.Start(email='user@example.com',game_closed=True),factory(request_handler))
    with pytest.raises(HTTPException) as e:ea_auth.session(engine,a['account_id'])
    assert e.value.detail=='EA_PIN_PENDING'
    with engine.begin() as c:
        v,row=ea_login.load_locked(c,a['account_id']);v['challenge']['submitted']=True;ea_login.save(c,v,row,'pin_verifying')
    with pytest.raises(HTTPException) as e:ea_login.finish(engine,a['account_id'],ea_login.Finish(challenge_id=d['challenge_id'],pin='123456',game_closed=True),factory(lambda r:pytest.fail('Unknown PIN replay')))
    assert e.value.detail=='EA_PIN_ALREADY_SUBMITTED'

def test_routes_require_control_and_never_echo_pin(engine,monkeypatch):
    client,a,_,_=configured(engine,monkeypatch);read={'Authorization':'Bearer '+a['read_token']}
    assert client.get('/ea-login').status_code==200
    assert client.get('/ea-login').headers['cache-control']=='no-store'
    assert client.get('/v1/ea-login').status_code==401
    assert client.get('/v1/ea-login',headers=read).status_code==403
    control=client.post(f'/v1/accounts/{a["account_id"]}/control-key',headers={'Authorization':'Bearer '+'A'*48}).json()['control_token'];headers={'Authorization':'Bearer '+control}
    assert client.post('/v1/ea-login/start',headers=headers,json={'email':'user@example.com','game_closed':False}).status_code==409
    r=client.post('/v1/ea-login/finish',headers=headers,json={'challenge_id':'a'*24,'pin':'sensitive-invalid-pin','game_closed':True})
    assert r.status_code==422 and 'sensitive-invalid-pin' not in r.text
    assert client.get('/v1/ea-login',headers=headers).json()['state']=='not_initialized'
