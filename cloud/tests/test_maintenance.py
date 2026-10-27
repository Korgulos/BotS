import json,uuid
from datetime import timedelta
import httpx
from sqlalchemy import select
from test_api import setup
from swgoh_cloud import db
from swgoh_cloud.direct_wire import field
from swgoh_cloud.session_wire import fields
from swgoh_cloud.reward_wire import Rewards
from swgoh_cloud.maintenance import rewards,SESSION
from swgoh_cloud.local_session import private_write
from swgoh_cloud.local_maintenance import tick

def initial(with_message=True,bonus=100,kind=4):
    reward=field(1,'GRIND')+field(2,3)+field(4,100)+field(5,100)
    m=field(1,'private-inbox-id')+field(2,'ARENA_REWARD')+field(6,2000000000)+field(7,reward)+field(8,kind)+field(11,1)
    player=field(19,123)+field(34,field(1,1)+field(2,bonus))
    return field(2,player)+field(5,b'')+field(16,field(1,m) if with_message else b'')

def context(engine):
    client,admin,a,s=setup(engine)
    return client,a,s,{'_accountId':a['account_id'],'_sourceId':s['source_id'],'expectedAllyCode':123,'authId':'private-auth-id','authToken':'private-auth-token','capturedAt':db.now().isoformat(),'clientTime':1234567890}

def test_reward_packet_exact_accept_and_allowlist():
    codec=Rewards();m=codec.inbox(initial())[0]
    envelope=fields(codec.packet({'authId':'id','authToken':'token','capturedAt':db.now().isoformat(),'clientTime':123},'UpdateInbox',m));body=fields(envelope[6][0])
    assert body[1]==[b'private-inbox-id'] and body[2]==[0] and body[4]==[4] and 3 not in body
    assert fields(codec.packet({'authId':'id','authToken':'token','capturedAt':db.now().isoformat(),'clientTime':123},'ClaimAllEnergyRewards'))[6]==[b'']
    try:codec.packet({},'BuyShipmentItem')
    except ValueError:pass
    else:assert False
    try:codec.packet({},'UpdateInbox',codec.inbox(initial(kind=1))[0])
    except ValueError:pass
    else:assert False

def test_rewards_claim_once_refresh_and_redact(engine):
    api,a,source,s=context(engine);calls=[];claimed=False;energy=False
    def handler(request):
        nonlocal claimed,energy
        envelope=fields(request.content);operation=envelope[5][0].decode();calls.append(operation)
        if operation=='GetInitialData':body=initial(not claimed,200 if energy else 100)
        elif operation=='UpdateInbox':claimed=True;body=b''
        elif operation=='ClaimAllEnergyRewards':energy=True;body=b''
        else:raise AssertionError(operation)
        return httpx.Response(200,content=field(5,1)+field(4,body))
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result=rewards(engine,s,client);assert result['state']=='ok' and result['inbox']['claimed']==1 and result['energy']['bonus_changed']
        rewards(engine,s,client)
    assert calls.count('UpdateInbox')==1 and calls.count('ClaimAllEnergyRewards')==1
    with engine.connect() as c:
        stored=json.dumps([x[0] for x in c.execute(select(db.events.c.payload))]);assert 'private-inbox-id' not in stored and 'private-auth' not in stored
    rows=api.get('/v1/snapshots',headers={'Authorization':'Bearer '+a['read_token']}).json()
    assert next(x for x in rows if x['topic']=='inbox')['payload']['inbox']['messages']==[]

def test_uncertain_send_not_repeated(engine):
    _,_,_,s=context(engine);calls=[]
    def handler(request):
        operation=fields(request.content)[5][0].decode();calls.append(operation)
        if operation=='GetInitialData':return httpx.Response(200,content=field(5,1)+field(4,initial()))
        raise httpx.ReadTimeout('credentials must never reach logs')
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert rewards(engine,s,client)['state']=='uncertain'
        assert rewards(engine,s,client)['state']=='uncertain'
    assert calls.count('UpdateInbox')==1 and 'ClaimAllEnergyRewards' not in calls

def test_local_timer_uses_private_config_hourly_request_and_skips_game(tmp_path):
    p=tmp_path/'control.json';private_write(p,{'cloud_url':'https://swgoh-inspector-cloud.onrender.com','account_id':'account','control_token':'private-token'})
    ids=[]
    def handler(r):
        assert r.url.path=='/v1/maintenance/run';ids.append(json.loads(r.content)['request_id']);return httpx.Response(200,json={'state':'ok','refreshed':True,'private':'never printed'})
    factory=lambda **kw:httpx.Client(transport=httpx.MockTransport(handler))
    result=tick(p,client_factory=factory,running=lambda:False);assert result['state']=='ok' and 'private' not in result
    tick(p,client_factory=factory,running=lambda:False);assert ids[0]==ids[1]
    assert tick(p,client_factory=factory,running=lambda:True)['sent'] is False
    assert (tmp_path/'maintenance-last.local.json').stat().st_mode&0o777==0o600

def test_maintenance_run_is_account_bound_durable_and_deduplicated(engine,monkeypatch):
    from swgoh_cloud import maintenance as module
    _,a,source,s=context(engine);s.update(requestTokenMode='absent',_expiresAt=(db.now()+timedelta(hours=12)).isoformat());monkeypatch.setenv('SWGOH_TERRITORY_SESSION',json.dumps(s))
    monkeypatch.setattr(module,'tb_status',lambda *a:{'state':'ok'});monkeypatch.setattr(module,'run_due',lambda *a,**kw:[])
    count=[];request_id=uuid.uuid4();who={'account_id':a['account_id']}
    def collector(*args):
        count.append(True)
        assert module.run(engine,who,uuid.uuid4(),collector=lambda *a:(_ for _ in ()).throw(AssertionError()))['state']=='already_running'
        return {'state':'ok','inbox':{'claimed':0},'energy':{'state':'ok'},'refreshed':True}
    result=module.run(engine,who,request_id,collector=collector)
    assert result['state']=='ok' and module.run(engine,who,request_id,collector=collector)==result and len(count)==1
    s['_expiresAt']=(db.now()-timedelta(seconds=1)).isoformat();monkeypatch.setenv('SWGOH_TERRITORY_SESSION',json.dumps(s))
    assert module.run(engine,who,uuid.uuid4(),collector=collector)['state']=='session_unavailable' and len(count)==1
