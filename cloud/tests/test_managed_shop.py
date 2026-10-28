import json
from datetime import timedelta
import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_direct_purchases import ready
from swgoh_cloud import ea_auth,db,direct_purchases,shipments
from swgoh_cloud.direct_wire import field

def managed(engine,monkeypatch):
    client,owner,writer,reader,s,body,fresh=ready(engine,monkeypatch)
    seed=dict(seed_id='shop-seed',account_id=s['_accountId'],source_id=s['_sourceId'],expected_ally_code=s['expectedAllyCode'],cookies=[{'name':'remid','value':'private'}],template={'unused':True},auth_body='unused')
    monkeypatch.setenv(ea_auth.KEY,Fernet.generate_key().decode());monkeypatch.setenv(ea_auth.SEED,ea_auth.seal(seed))
    value={**seed,'session':{**s,'_expiresAt':(db.now()+timedelta(hours=2)).isoformat()}}
    with engine.begin() as c:c.execute(db.auth_sessions.insert().values(account_id=s['_accountId'],source_id=s['_sourceId'],seed_id=seed['seed_id'],sealed=ea_auth.seal(value),state='ready',updated_at=db.now()))
    return client,owner,reader,s,body,fresh

def test_managed_session_used_for_quote_and_rotated_session_blocks_confirmation(engine,monkeypatch):
    c,owner,reader,s,body,_=managed(engine,monkeypatch)
    monkeypatch.delenv('SWGOH_DIRECT_TEST_SESSION')
    status=c.get('/v1/purchases',headers=owner).json()
    assert status['direct']['available'] and status['can_purchase']
    command=c.post('/v1/direct/purchases/prepare',headers=owner,json=body).json()
    assert command['state']=='prepared'
    with engine.begin() as conn:
        row=conn.execute(select(db.auth_sessions)).mappings().one();value=ea_auth.unseal(row['sealed']);value['session']['authToken']='rotated-token';conn.execute(db.auth_sessions.update().values(sealed=ea_auth.seal(value)))
    monkeypatch.setattr(direct_purchases,'receive',lambda *args:(_ for _ in ()).throw(AssertionError('Must not purchase')))
    response=c.post('/v1/direct/purchases/'+command['id']+'/confirm',headers=owner,json={'quote_hash':command['quote_hash'],'confirmed':True})
    assert response.status_code==409

def test_expired_cached_session_advertises_renewal_without_network(engine,monkeypatch):
    c,owner,_,s,_,_=managed(engine,monkeypatch)
    with engine.begin() as conn:
        row=conn.execute(select(db.auth_sessions)).mappings().one();value=ea_auth.unseal(row['sealed']);value['session']['_expiresAt']=(db.now()-timedelta(seconds=1)).isoformat();conn.execute(db.auth_sessions.update().values(sealed=ea_auth.seal(value)))
    monkeypatch.setattr(ea_auth,'renew',lambda *args:(_ for _ in ()).throw(AssertionError('GET must not log in')))
    d=c.get('/v1/purchases',headers=owner).json()['direct']
    assert d['available'] and d['renewable'] and d['expires_at'] is None

def wire(ally,refresh):
    reward=field(1,'GEAR')+field(2,11)+field(4,6)+field(5,6)
    cost=field(1,'GRIND')+field(2,3)+field(4,2700)+field(5,2700)
    item=field(12,'OFFER1')+field(13,reward)+field(14,cost)
    board=field(1,2)+field(2,item)+field(3,refresh)
    return field(2,field(19,ally))+field(18,field(10,board))

def test_server_refresh_saves_fresh_offers_and_only_reads(engine,monkeypatch):
    c,owner,reader,s,_,_=managed(engine,monkeypatch);calls=[]
    payload=wire(s['expectedAllyCode'],int(db.now().timestamp())+3600)
    def receive(client,packet):
        from swgoh_cloud.session_wire import fields
        assert fields(packet)[5]==[b'GetInitialData'];calls.append(True);return 1,payload
    monkeypatch.setattr(shipments,'receive',receive)
    assert c.post('/v1/shipments/refresh',headers=reader).status_code==403
    r=c.post('/v1/shipments/refresh',headers=owner);assert r.status_code==200,r.text
    assert r.json()['offers']==1 and len(calls)==1
    data=c.get('/v1/shipments',headers=reader).json();assert data[0]['offer_id']=='OFFER1' and data[0]['purchased'] is False
    assert c.post('/v1/shipments/refresh',headers=owner).status_code==429 and len(calls)==1

def test_mismatched_refresh_preserves_snapshot(engine,monkeypatch):
    c,owner,reader,s,_,_=managed(engine,monkeypatch)
    before=c.get('/v1/shipments',headers=reader).json()
    monkeypatch.setattr(shipments,'receive',lambda *args:(1,wire(999,int(db.now().timestamp())+3600)))
    assert c.post('/v1/shipments/refresh',headers=owner).status_code==409
    assert c.get('/v1/shipments',headers=reader).json()==before
