import base64,json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import httpx,pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException
from sqlalchemy import select
from test_api import setup
from swgoh_cloud import db,ea_auth
from swgoh_cloud.direct_wire import field
from swgoh_cloud.session_wire import fields

def configured(engine,monkeypatch):
    client,admin,a,source=setup(engine)
    seed=dict(seed_id='seed-one',account_id=a['account_id'],source_id=source['source_id'],expected_ally_code=123,
        cookies=[dict(name='remid',value='private-cookie',domain='.ea.com',path='/')],
        template=dict(capturedAt=db.now().isoformat(),clientTime=1234567890000,authId='discard',authToken='discard'),
        auth_body=base64.b64encode(field(2,'en')+field(6,'guest')).decode())
    monkeypatch.setenv(ea_auth.KEY,Fernet.generate_key().decode());monkeypatch.setenv(ea_auth.SEED,ea_auth.seal(seed))
    return client,a,source,seed

def test_jws_login_consumes_bootstrap_token_and_verifies_account():
    observed=[]
    value=dict(account_id='a',source_id='s',expected_ally_code=123,cookies=[],template=dict(capturedAt=db.now().isoformat(),clientTime=1234567890000),auth_body=base64.b64encode(field(2,'en')).decode())
    def handler(request):
        if request.url.host=='accounts.ea.com':
            assert request.url.params['token_format']=='JWS'
            return httpx.Response(200,json={'access_token':'header.payload.signature','expires_in':'14399'},headers={'set-cookie':'remid=rotated; Domain=.ea.com; Secure; Path=/'})
        envelope=fields(request.content);op=envelope[5][0].decode();observed.append(op)
        if op=='DoAuthNucleus':
            assert 7 not in envelope and 8 not in envelope
            body=fields(envelope[6][0]);assert body[3]==[1] and body[10]==[0]
            result=field(5,1)+field(4,field(1,'auth-id')+field(2,'auth-secret'))+field(13,'bootstrap')
        elif op=='GetInitialData':
            assert envelope[36]==[b'bootstrap'];result=field(5,1)+field(4,field(2,field(19,123)))
        else:
            assert op=='Ping' and 36 not in envelope;result=field(5,1)
        return httpx.Response(200,content=result)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:session=ea_auth.renew(value,c)
    assert observed==['DoAuthNucleus','GetInitialData','Ping']
    assert session['requestTokenMode']=='absent' and session['_accountId']=='a'
    assert value['cookies'][0]['value']=='rotated'

def test_encrypted_cache_survives_restart_and_concurrent_callers(engine,monkeypatch):
    client,a,source,seed=configured(engine,monkeypatch);calls=[]
    def renew(value,client):
        calls.append(True);s=dict(_expiresAt=(db.now()+timedelta(hours=2)).isoformat(),authToken='private-game-token');value['session']=s;return s
    monkeypatch.setattr(ea_auth,'renew',renew)
    with ThreadPoolExecutor(2) as pool:results=list(pool.map(lambda _:ea_auth.session(engine,a['account_id']),range(2)))
    assert len(calls)==1 and results[0]==results[1]
    with engine.connect() as c:row=c.execute(select(db.auth_sessions)).mappings().one()
    assert 'private-cookie' not in row['sealed'] and 'private-game-token' not in row['sealed']
    text=client.get('/v1/events',headers={'Authorization':'Bearer '+a['read_token']}).text
    assert 'private-game-token' not in text and 'private-cookie' not in text
    assert ea_auth.session(engine,a['account_id'])==results[0] and len(calls)==1

def test_failure_saves_rotation_and_backoff_without_leaking_exception(engine,monkeypatch):
    _,a,_,_=configured(engine,monkeypatch);called=[]
    def renew(value,client):
        called.append(True);value['cookies'][0]['value']='rotated-private';raise RuntimeError('credential-bearing upstream text')
    monkeypatch.setattr(ea_auth,'renew',renew)
    for expected in ['EA_AUTH_FAILED','EA_AUTH_RETRY_LATER']:
        with pytest.raises(HTTPException) as e:ea_auth.session(engine,a['account_id'])
        assert e.value.detail==expected
    assert len(called)==1
    with engine.connect() as c:row=c.execute(select(db.auth_sessions)).mappings().one()
    assert ea_auth.unseal(row['sealed'])['cookies'][0]['value']=='rotated-private'
    assert row['state']=='EA_AUTH_FAILED'

def test_online_bridge_blocks_renewal_and_other_account_never_uses_seed(engine,monkeypatch):
    _,a,source,_=configured(engine,monkeypatch)
    with engine.begin() as c:c.execute(db.agents.insert().values(source_id=source['source_id'],session='game',last_seen=db.now(),online=True,purchases_enabled=False))
    monkeypatch.setattr(ea_auth,'renew',lambda *args:pytest.fail('Network must not run'))
    with pytest.raises(HTTPException) as e:ea_auth.session(engine,a['account_id'])
    assert e.value.detail=='EA_GAME_OR_TRANSACTION_ACTIVE'
    assert ea_auth.session(engine,'another-account') is None

def test_account_mismatch_stops_before_ping():
    value=dict(account_id='a',source_id='s',expected_ally_code=123,cookies=[],template=dict(capturedAt=db.now().isoformat(),clientTime=1234567890000),auth_body=base64.b64encode(field(2,'en')).decode())
    calls=[]
    def handler(request):
        if request.url.host=='accounts.ea.com':return httpx.Response(200,json={'access_token':'a.b.c','expires_in':14000})
        op=fields(request.content)[5][0];calls.append(op)
        body=field(1,'id')+field(2,'token') if op==b'DoAuthNucleus' else field(2,field(19,999))
        return httpx.Response(200,content=field(5,1)+field(4,body))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        with pytest.raises(ea_auth.AuthFailure,match='ACCOUNT_MISMATCH'):ea_auth.renew(value,c)
    assert calls==[b'DoAuthNucleus',b'GetInitialData']

def test_renew_route_requires_control_and_closed_game(engine,monkeypatch):
    client,a,_,_=configured(engine,monkeypatch)
    control=client.post(f'/v1/accounts/{a["account_id"]}/control-key',headers={'Authorization':'Bearer '+'A'*48}).json()['control_token']
    assert client.post('/v1/territory/session/renew',json={'game_closed':False},headers={'Authorization':'Bearer '+control}).status_code==409
    assert client.post('/v1/territory/session/renew',json={'game_closed':True}).status_code==401
    assert client.post('/v1/territory/session/renew',json={'game_closed':True},headers={'Authorization':'Bearer '+a['read_token']}).status_code==403
    result=client.get('/v1/territory/session',headers={'Authorization':'Bearer '+a['read_token']})
    assert result.status_code==200 and 'private-cookie' not in result.text
