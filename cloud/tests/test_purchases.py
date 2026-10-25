from datetime import timedelta
import json
from uuid import uuid4
import httpx
import pytest
from sqlalchemy import select
from test_api import setup, observation
from swgoh_cloud import db
from swgoh_cloud.bridge import Outbox
from swgoh_cloud.purchase_worker import PurchaseWorker

SESSION='1'*64

def prepare_fixture(engine):
    c,a,account,source=setup(engine)
    control=c.post('/v1/accounts/'+account['account_id']+'/control-key',headers=a).json()['control_token']
    owner={'Authorization':'Bearer '+control};writer={'Authorization':'Bearer '+source['ingest_token']};reader={'Authorization':'Bearer '+account['read_token']}
    e=observation(session=SESSION)
    e['payload']['offers']['shipments'][0]['items'][0]['cost']['type']='CURRENCY'
    e['payload']['offers']['shipments'][0]['nextRefresh']=str(int(db.now().timestamp())+3600)
    assert c.post('/v1/ingest',headers=writer,json={'events':[e]}).status_code==200
    event=c.get('/v1/snapshots',headers=reader).json()[0]['event_id']
    body=dict(request_id=str(uuid4()),event_id=event,ordinal=0)
    assert c.post('/v1/bridge/heartbeat',headers=writer,json=dict(session=SESSION,online=True,purchases_enabled=True)).status_code==200
    return c,a,owner,writer,reader,body,source


def test_roles_quote_confirmation_and_replay(engine):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    assert c.post('/v1/purchases/prepare',headers=r,json=b).status_code==403
    assert c.post('/v1/purchases/prepare',headers=w,json=b).status_code==403
    assert c.post('/v1/purchases/prepare',headers=o,json={**b,'price':1}).status_code==422
    command=c.post('/v1/purchases/prepare',headers=o,json=b).json();identifier=command['id']
    assert c.post('/v1/purchases/prepare',headers=o,json=b).json()['id']==identifier
    assert c.post('/v1/purchases/prepare',headers=o,json={**b,'request_id':str(uuid4())}).status_code==409
    claim='/v1/bridge/purchases/'+identifier+'/claim';report='/v1/bridge/purchases/'+identifier+'/report'
    assert c.post(claim,headers=w,json=dict(session=SESSION,phase='commit')).status_code==409
    assert c.post(claim,headers=w,json=dict(session=SESSION,phase='prepare')).status_code==200
    q={**command['expected'],'balance':5000,'balanceAfter':2300}
    rep=dict(session=SESSION,state='prepared',quote=q,expires_at=(db.now()+timedelta(seconds=30)).isoformat())
    assert c.post(report,headers=w,json={**rep,'quote':{**q,'price':1}}).status_code==409
    ready=c.post(report,headers=w,json=rep).json();assert ready['state']=='prepared'
    confirm='/v1/purchases/'+identifier+'/confirm'
    assert c.post(confirm,headers=o,json=dict(quote_hash='0'*64,confirmed=True)).status_code==409
    data=dict(quote_hash=ready['quote_hash'],confirmed=True)
    assert c.post(confirm,headers=r,json=data).status_code==403
    assert c.post(confirm,headers=o,json=data).json()['state']=='queued_commit'
    assert c.post(claim,headers=w,json=dict(session=SESSION,phase='commit')).json()['state']=='sending'
    assert c.post(report,headers=w,json=dict(session=SESSION,state='succeeded',code='OK')).json()['state']=='succeeded'
    assert c.post(confirm,headers=o,json=data).json()['state']=='succeeded'
    with engine.connect() as x:assert len(x.execute(select(db.purchases)).all())==1


def test_crystals_expired_offline_and_source_isolation(engine):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    with engine.begin() as x:x.execute(db.offers.update().values(price=dict(type='CURRENCY',definition='PREMIUM',minQuantity=1)))
    assert c.post('/v1/purchases/prepare',headers=o,json=b).status_code==409
    with engine.begin() as x:x.execute(db.offers.update().values(price=dict(type='CURRENCY',definition='GRIND',minQuantity=2700)))
    c.post('/v1/bridge/heartbeat',headers=w,json=dict(session=SESSION,online=False,purchases_enabled=True))
    assert c.post('/v1/purchases/prepare',headers=o,json=b).status_code==409
    c.post('/v1/bridge/heartbeat',headers=w,json=dict(session=SESSION,online=True,purchases_enabled=True))
    cmd=c.post('/v1/purchases/prepare',headers=o,json=b).json()
    other=c.post('/v1/accounts',headers=a,json={'alias':'other'}).json()
    s=c.post('/v1/accounts/'+other['account_id']+'/sources',headers=a,json={'name':'other'}).json()
    assert c.post('/v1/bridge/purchases/'+cmd['id']+'/claim',headers={'Authorization':'Bearer '+s['ingest_token']},json=dict(session=SESSION,phase='prepare')).status_code==404
    with engine.begin() as x:x.execute(db.purchases.update().values(expires_at=db.now()-timedelta(seconds=1)))
    assert c.post('/v1/bridge/purchases/'+cmd['id']+'/claim',headers=w,json=dict(session=SESSION,phase='prepare')).status_code==409
    assert c.get('/v1/purchases',headers=r).json()['commands'][0]['state']=='expired'


@pytest.mark.parametrize('lost',['native_commit','cloud_claim'])
def test_worker_never_replays_commit_across_restart(engine,tmp_path,lost):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    cmd=c.post('/v1/purchases/prepare',headers=o,json=b).json()
    queue=Outbox(tmp_path/'outbox.sqlite3');worker=PurchaseWorker(queue.conn)
    sends=[];fault=[False]
    def transport(request):
        if request.url.host=='cloud.invalid':
            assert b'A'*48 not in request.content  # Native approval token never enters the cloud.
            data=json.loads(request.content) if request.content else None
            result=c.request(request.method,request.url.path,headers={'Authorization':request.headers['Authorization']},json=data)
            if lost=='cloud_claim' and request.url.path.endswith('/claim') and data.get('phase')=='commit' and not fault[0]:
                fault[0]=True;raise httpx.ReadTimeout('ACK lost',request=request)
            return httpx.Response(result.status_code,content=result.content,headers={'content-type':'application/json'})
        assert request.url.host=='127.0.0.1'
        assert request.headers['Origin']=='http://127.0.0.1:18181'
        if request.url.path=='/api/purchase/prepare':
            return httpx.Response(200,json={'token':'A'*48,'expiresAt':(db.now()+timedelta(seconds=30)).isoformat(),'quote':{**cmd['expected'],'balance':5000,'balanceAfter':2300}})
        if request.url.path=='/api/purchase/commit':
            sends.append(1)
            if lost=='native_commit' and not fault[0]:
                fault[0]=True;raise httpx.ReadTimeout('response lost',request=request)
            return httpx.Response(200,json={'state':'sending'})
        assert request.method=='GET' and request.url.path=='/api/purchase/status'
        return httpx.Response(200,json={'state':'succeeded','code':'OK'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        tick=lambda:worker.tick(client,'https://cloud.invalid',source['ingest_token'],'http://127.0.0.1:18181','K'*48,SESSION,True,True)
        tick();tick();assert sends==[]
        ready=c.get('/v1/purchases',headers=r).json()['commands'][0];assert ready['state']=='prepared'
        c.post('/v1/purchases/'+cmd['id']+'/confirm',headers=o,json=dict(confirmed=True,quote_hash=ready['quote_hash']))
        try:tick()
        except httpx.ReadTimeout:pass
        # Reopen the durable journal exactly as a newly started bridge does.
        queue.close();queue=Outbox(tmp_path/'outbox.sqlite3');worker=PurchaseWorker(queue.conn)
        tick();tick();assert len(sends)==1
        assert c.get('/v1/purchases',headers=r).json()['commands'][0]['state']=='succeeded'
    queue.close()


def test_uncertain_result_blocks_new_purchase(engine):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    command=c.post('/v1/purchases/prepare',headers=o,json=b).json()
    with engine.begin() as x:x.execute(db.purchases.update().where(db.purchases.c.id==command['id']).values(state='uncertain'))
    assert c.post('/v1/purchases/prepare',headers=o,json={**b,'request_id':str(uuid4())}).status_code==409

@pytest.mark.parametrize('stars,secondaries,expected',[
    (5,[{'stat':'SPEED','value':5}],True),
    (4,[{'stat':'SPEED','value':5}],False),
    (6,[{'stat':'SPEED','value':5}],False),
    (5,[{'stat':'HEALTH','value':500}],False),
    (5,[{'stat':'SPEED','value':0}],False),
    (5,[],False),
])
def test_only_five_star_mods_with_positive_secondary_speed(stars,secondaries,expected):
    from swgoh_cloud.mod_policy import eligible_mod
    reward={'mod':{'verified':True,'stars':stars,'secondaries':secondaries,'primary':{'stat':'SPEED','value':30}}}
    assert eligible_mod(reward) is expected
    assert not eligible_mod({'mod':{'verified':False,'stars':5,'secondaries':[{'stat':'SPEED','value':5}]}})
    assert not eligible_mod({})


def test_purchase_api_blocks_unverified_and_wrong_mods(engine):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    with engine.begin() as x:x.execute(db.offers.update().values(shop='SHIPMENTMOD',reward={'type':'STATMOD','minQuantity':1,'definition':'mod'}))
    assert c.post('/v1/purchases/prepare',headers=o,json=b).status_code==409
    with engine.begin() as x:x.execute(db.offers.update().values(reward={'type':'STATMOD','minQuantity':1,'definition':'mod','mod':{'verified':True,'stars':5,'secondaries':[{'stat':'SPEED','value':5}]}}))
    assert c.post('/v1/purchases/prepare',headers=o,json=b).status_code==200
