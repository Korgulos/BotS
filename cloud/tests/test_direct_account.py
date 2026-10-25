import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from test_purchases import prepare_fixture
from swgoh_cloud import db,direct_account


def fixture(engine,monkeypatch):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    with engine.begin() as x:
        row=x.execute(select(db.sources)).mappings().first();account=row['account_id'];sid=row['id']
        x.execute(db.agents.update().values(online=False))
    s=dict(_accountId=account,_sourceId=sid,_testId='a'*32,capturedAt=(db.now()-timedelta(minutes=1)).isoformat(),_expiresAt=(db.now()+timedelta(minutes=5)).isoformat(),requestTokenMode='absent',authId='SECRET_ID',authToken='SECRET_TOKEN',expectedAllyCode=392189647)
    monkeypatch.setenv('SWGOH_DIRECT_TEST_SESSION',json.dumps(s))
    return c,o,w,r,s


def test_direct_read_roles_order_cache_and_no_credentials_in_db(engine,monkeypatch):
    c,o,w,r,s=fixture(engine,monkeypatch);calls=[]
    def exchange(session,op,client):
        calls.append(op);assert session['authToken']=='SECRET_TOKEN'
        return {'operation':op,'success':True,'rpc_code':1,'data':{'inventory':{'units':412}}}
    monkeypatch.setattr(direct_account,'exchange',exchange)
    body={'operation':'Ping','game_closed':True}
    for key in [r,w]:assert c.post('/v1/direct/account/read',headers=key,json=body).status_code==403
    assert c.post('/v1/direct/account/read',headers=o,json={**body,'operation':'BuyShipmentItem'}).status_code==422
    assert c.post('/v1/direct/account/read',headers=o,json={**body,'game_closed':False}).status_code==422
    assert c.post('/v1/direct/account/read',headers=o,json={**body,'operation':'GetInitialData'}).status_code==409
    for op in ['Ping','GetInitialData']:
        for _ in range(2):assert c.post('/v1/direct/account/read',headers=o,json={**body,'operation':op}).json()['success']
    assert calls==['Ping','GetInitialData']
    with engine.connect() as x:
        rows=[dict(r) for r in x.execute(select(db.events)).mappings()]
    assert 'SECRET' not in str(rows)


def test_direct_read_uncertain_never_replayed(engine,monkeypatch):
    c,o,w,r,s=fixture(engine,monkeypatch);calls=[]
    def exchange(*args):calls.append(1);raise ValueError('SECRET_TOKEN')
    monkeypatch.setattr(direct_account,'exchange',exchange)
    body={'operation':'Ping','game_closed':True}
    result=c.post('/v1/direct/account/read',headers=o,json=body)
    assert result.json()['outcome']=='uncertain' and 'SECRET' not in result.text
    assert c.post('/v1/direct/account/read',headers=o,json=body).status_code==409
    assert calls==[1]


@pytest.mark.parametrize('change',[{'_accountId':'other'},{'_sourceId':'other'},{'_expiresAt':'2000-01-01T00:00:00+00:00'},{'requestTokenMode':'next'},{'requestToken':'SECRET'}])
def test_direct_session_scope_expiry_and_sequence(engine,monkeypatch,change):
    c,o,w,r,s=fixture(engine,monkeypatch);s.update(change);monkeypatch.setenv('SWGOH_DIRECT_TEST_SESSION',json.dumps(s))
    assert c.post('/v1/direct/account/read',headers=o,json={'operation':'Ping','game_closed':True}).status_code==409
