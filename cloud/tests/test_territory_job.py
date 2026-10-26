import json
from datetime import timedelta
import httpx
from sqlalchemy import select
from test_api import setup
from test_territory import payload
from swgoh_cloud import db
from swgoh_cloud.territory_job import configure,run_due,status,SESSION,EVENT
from swgoh_cloud.territory_wire import Codec,collect,SessionRejected
from swgoh_cloud.direct_wire import field
from swgoh_cloud.session_wire import fields

def test_wire_only_read_operations_and_explicit_tb_type():
    c=Codec();s={'capturedAt':db.now().isoformat(),'clientTime':1234567890,'authId':'id','authToken':'secret'}
    packet=fields(c.packet(s,'GetMapStats',{'territory_map_id':'map','event_type':9}))
    assert packet[4]==[b'TerritoryRpc'] and fields(packet[6][0])[3]==[9]
    try:c.packet(s,'BattleStart')
    except KeyError:pass
    else:assert False
    score=c.stats([field(1,'power_round_1')+field(2,field(1,'private-member')+field(2,2**63-1))],{})
    assert score['items'][0]['players']['items'][0]['score']==str(2**63-1)
    assert 'private-member' not in json.dumps(score)

def setup_job(engine,monkeypatch):
    client,admin,a,s=setup(engine)
    session={'_accountId':a['account_id'],'_sourceId':s['source_id'],'capturedAt':db.now().isoformat(),'_expiresAt':(db.now()+timedelta(hours=12)).isoformat(),'authId':'x','authToken':'secret','requestTokenMode':'absent','expectedAllyCode':123}
    monkeypatch.setenv('SWGOH_TERRITORY_SESSION',json.dumps(session));configure(engine,a['account_id'],True)
    return client,a,s,session

def test_hourly_durable_reservation_and_full_roster(engine,monkeypatch):
    client,a,s,session=setup_job(engine,monkeypatch);called=[]
    def collector(*args):
        assert run_due(engine,a['account_id'],collector=lambda *a:(_ for _ in ()).throw(AssertionError()))==[]
        called.append(True);return payload()
    assert run_due(engine,a['account_id'],collector=collector)==[{'state':'ok','saved':True}]
    assert run_due(engine,a['account_id'],collector=collector)==[] and len(called)==1
    p=status(engine,a['account_id']);assert p['last_success'] and p['lease_until'] is None
    data=client.get('/v1/territory/phases',headers={'Authorization':'Bearer '+a['read_token']}).json()
    assert len(data[0]['payload']['rows'])==47
    assert client.post('/v1/territory/collect',headers={'Authorization':'Bearer '+a['read_token']}).status_code==403
    assert client.post('/v1/territory/collect').status_code==401

def test_rejection_disables_and_expiry_never_calls_network(engine,monkeypatch):
    client,a,s,session=setup_job(engine,monkeypatch)
    def reject(*args):raise SessionRejected()
    assert run_due(engine,a['account_id'],collector=reject)[0]['state']=='session_rejected'
    assert status(engine,a['account_id'])['enabled'] is False
    session['_expiresAt']=(db.now()-timedelta(seconds=1)).isoformat();monkeypatch.setenv('SWGOH_TERRITORY_SESSION',json.dumps(session))
    # Disabling remains possible with an expired session.
    assert configure(engine,a['account_id'],False)['enabled'] is False

def test_online_game_skips_direct_calls(engine,monkeypatch):
    client,a,s,session=setup_job(engine,monkeypatch)
    with engine.begin() as c:c.execute(db.agents.insert().values(source_id=s['source_id'],session='test',last_seen=db.now(),online=True,purchases_enabled=False))
    assert run_due(engine,a['account_id'],collector=lambda *a:(_ for _ in ()).throw(AssertionError()))==[]
    assert status(engine,a['account_id'])['state']=='waiting_for_game_or_transaction'

def test_manual_collection_works_with_schedule_disabled_and_is_throttled(engine,monkeypatch):
    client,a,s,session=setup_job(engine,monkeypatch);configure(engine,a['account_id'],False)
    assert run_due(engine,a['account_id'],collector=lambda *a:payload(),force=True)==[{'state':'ok','saved':True}]
    assert status(engine,a['account_id'])['enabled'] is False
    assert run_due(engine,a['account_id'],collector=lambda *a:(_ for _ in ()).throw(AssertionError()),force=True)==[]
