import json
from datetime import timedelta
from sqlalchemy import select
from test_inbox_claim import fixture
from test_purchases import SESSION
from swgoh_cloud import db
from swgoh_cloud.inbox_automation import automatic_key
from swgoh_cloud.inbox_policy import preview

def tick(c,w):return c.post('/v1/bridge/inbox/auto-prepare',headers=w,json={'session':SESSION})
def enable(c,o,value=True):return c.post('/v1/inbox/automation',headers=o,json={'enabled':value})

def test_opt_in_roles_prepare_and_automatic_commit_only_for_inbox(engine):
    c,o,w,r,b,source,m=fixture(engine)
    assert not tick(c,w).json()['queued']
    for h in [w,r]:assert enable(c,h).status_code==403
    assert enable(c,o).json()['enabled']
    queued=tick(c,w).json();assert queued['queued']
    with engine.connect() as conn:cmd=dict(conn.execute(select(db.purchases)).mappings().one())
    assert cmd['expected']['kind']=='inbox' and cmd['expected']['automatic'] is True and cmd['confirmed_at'] is None
    assert not tick(c,w).json()['queued']
    url='/v1/bridge/purchases/'+cmd['id']
    assert c.post(url+'/claim',headers=w,json={'session':SESSION,'phase':'prepare'}).status_code==200
    response=c.post(url+'/report',headers=w,json={'session':SESSION,'state':'prepared','quote':preview(m),'expires_at':(db.now()+timedelta(seconds=30)).isoformat()})
    assert response.json()['state']=='queued_commit' and response.json()['confirmed_at']
    assert c.post(url+'/claim',headers=w,json={'session':SESSION,'phase':'commit'}).json()['state']=='sending'
    assert c.post(url+'/report',headers=w,json={'session':SESSION,'state':'succeeded','code':'OK'}).json()['state']=='succeeded'
    assert not tick(c,w).json()['queued']

def test_disable_cancels_unsent_and_unknown_outcomes_never_requeued(engine):
    c,o,w,r,b,source,m=fixture(engine);enable(c,o);queued=tick(c,w).json()
    assert enable(c,o,False).json()['enabled'] is False
    with engine.connect() as conn:assert conn.execute(select(db.purchases.c.state)).scalar()=='cancelled'
    enable(c,o);assert not tick(c,w).json()['queued']
    with engine.begin() as conn:conn.execute(db.purchases.update().values(state='uncertain'))
    assert not tick(c,w).json()['queued']

def test_unsupported_special_ambiguous_messages_and_renumbering(engine):
    c,o,w,r,b,source,m=fixture(engine);enable(c,o)
    assert automatic_key(m)==automatic_key({**m,'number':50,'claimReference':'different'})
    for messages in [[{**m,'hasActionLink':True}],[{**m,'hasUnverifiedLiveDetails':True}],[{**m,'type':'GUILDINVITE'}],[m,dict(m)]]:
        with engine.begin() as conn:conn.execute(db.snapshots.update().where(db.snapshots.c.topic=='inbox').values(payload={'inbox':{'messages':messages}}))
        assert not tick(c,w).json()['queued']

def test_manual_prior_attempt_and_other_source_block_automation(engine):
    c,o,w,r,b,source,m=fixture(engine)
    cmd=c.post('/v1/inbox/prepare',headers=o,json=b).json()
    with engine.begin() as conn:conn.execute(db.purchases.update().values(state='failed'))
    enable(c,o);assert not tick(c,w).json()['queued']

def test_policy_disabled_after_prepare_never_sends(engine):
    c,o,w,r,b,source,m=fixture(engine);enable(c,o);identifier=tick(c,w).json()['id'];url='/v1/bridge/purchases/'+identifier
    c.post(url+'/claim',headers=w,json={'session':SESSION,'phase':'prepare'})
    enable(c,o,False)
    response=c.post(url+'/report',headers=w,json={'session':SESSION,'state':'prepared','quote':preview(m),'expires_at':(db.now()+timedelta(seconds=30)).isoformat()})
    assert response.json()['state']=='cancelled'
    assert c.post(url+'/claim',headers=w,json={'session':SESSION,'phase':'commit'}).status_code==409

def test_automatic_worker_claims_once_without_human_confirmation(engine,monkeypatch,tmp_path):
    import httpx
    from swgoh_cloud.bridge import Outbox
    from swgoh_cloud.purchase_worker import PurchaseWorker
    c,o,w,r,b,source,m=fixture(engine);enable(c,o);sends=[]
    def transport(request):
        if request.url.host=='cloud.invalid':
            assert b'B'*48 not in request.content
            result=c.request(request.method,request.url.path,headers={'Authorization':request.headers['Authorization']},json=json.loads(request.content) if request.content else None)
            return httpx.Response(result.status_code,content=result.content)
        if request.url.path=='/api/inbox':return httpx.Response(200,json={'inbox':{'messages':[{**m,'claimReference':'B'*48}]}})
        if request.url.path=='/api/inbox/claim/prepare':
            q=preview(m);q.pop('kind');q['reference']='B'*48
            return httpx.Response(200,json={'token':'A'*48,'quote':q,'expiresAt':(db.now()+timedelta(seconds=30)).isoformat()})
        if request.url.path=='/api/inbox/claim/commit':
            sends.append(1);assert json.loads(request.content)['confirmed'] is True
            raise httpx.ReadTimeout('lost response',request=request)
        assert request.url.path=='/api/inbox/claim/status' and request.method=='GET'
        return httpx.Response(200,json={'state':'succeeded','code':'OK'})
    outbox=Outbox(tmp_path/'auto.sqlite');worker=PurchaseWorker(outbox.conn)
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        for _ in range(5):worker.tick(client,'https://cloud.invalid',source['ingest_token'],'http://127.0.0.1:18181','K'*48,SESSION,True,True)
    with engine.connect() as conn:
        row=conn.execute(select(db.purchases)).mappings().one();assert row['state']=='succeeded' and row['expected']['automatic'] is True
    assert sends==[1]
    outbox.close()
