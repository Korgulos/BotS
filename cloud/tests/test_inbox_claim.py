import json
from datetime import timedelta
from uuid import uuid4
import httpx,pytest
from sqlalchemy import select
from test_purchases import prepare_fixture,SESSION
from swgoh_cloud import db
from swgoh_cloud.bridge import Outbox
from swgoh_cloud.purchase_worker import PurchaseWorker
from swgoh_cloud.inbox_policy import preview


def fixture(engine):
    c,a,o,w,r,b,source=prepare_fixture(engine)
    message=dict(type='RANK_REWARDS',subjectKey='RANK_SUBJECT',starts=str(int(db.now().timestamp())-10),expires=str(int(db.now().timestamp())+3600),attachmentCount=1,attachments=[dict(type='CURRENCY',definition='PREMIUM',minQuantity=220,maxQuantity=220)],number=1)
    with engine.begin() as x:
        snap=dict(x.execute(select(db.snapshots)).mappings().first())
        x.execute(db.snapshots.insert().values(**{**snap,'topic':'inbox','payload':{'inbox':{'messages':[message]}}}))
    return c,o,w,r,b,source,message

@pytest.mark.parametrize('type_subject',[('RANK_REWARDS','RANK_SUBJECT'),('TERRITORYTOURNAMENTMATCHREWARD','TERRITORYTOURNAMENT_REWARD_NO_CHANGE_FIRST_SUBJECT_KEY'),('PARAMETERIZED','RAID_REWARD_SUBJECT_KEY'),('GUILDRANKEDREWARD','GUILD_CHALLENGE_REWARD_SUBJECT_KEY'),('TERRITORYWARCOMPLETEREWARD','TERRITORY_WAR_SECOND_PLACE_SUBJECT_KEY')])
@pytest.mark.parametrize('lost',['native_commit','cloud_claim','changed_preview'])
def test_inbox_claim_confirmation_and_restart_never_replays(engine,tmp_path,lost,type_subject):
    c,o,w,r,b,source,m=fixture(engine)
    m['type'],m['subjectKey']=type_subject
    with engine.begin() as x:x.execute(db.snapshots.update().where(db.snapshots.c.topic=='inbox').values(payload={'inbox':{'messages':[m]}}))
    assert c.post('/v1/inbox/prepare',headers=r,json=b).status_code==403
    cmd=c.post('/v1/inbox/prepare',headers=o,json=b).json()
    assert cmd['expected']['preview']['rewards'][0]['definition']=='PREMIUM'
    q=Outbox(tmp_path/'inbox.sqlite');worker=PurchaseWorker(q.conn);sends=[];fault=[False]
    def transport(request):
        if request.url.host=='cloud.invalid':
            assert b'A'*48 not in request.content and b'B'*48 not in request.content
            data=json.loads(request.content) if request.content else None
            result=c.request(request.method,request.url.path,headers={'Authorization':request.headers['Authorization']},json=data)
            if lost=='cloud_claim' and request.url.path.endswith('/claim') and data.get('phase')=='commit' and not fault[0]:
                fault[0]=True;raise httpx.ReadTimeout('ACK lost',request=request)
            return httpx.Response(result.status_code,content=result.content)
        assert request.headers['Origin']=='http://127.0.0.1:18181'
        if request.url.path=='/api/inbox':
            changed={**m,'subjectKey':'CHANGED'} if lost=='changed_preview' else m
            return httpx.Response(200,json={'inbox':{'messages':[{**changed,'claimReference':'B'*48}]}})
        if request.url.path=='/api/inbox/claim/prepare':
            assert json.loads(request.content)=={'reference':'B'*48}
            p=preview(m);p.pop('kind');p['reference']='B'*48
            return httpx.Response(200,json={'token':'A'*48,'quote':p,'expiresAt':(db.now()+timedelta(seconds=30)).isoformat()})
        if request.url.path=='/api/inbox/claim/commit':
            sends.append(1)
            if lost=='native_commit' and not fault[0]:
                fault[0]=True;raise httpx.ReadTimeout('lost',request=request)
            return httpx.Response(200,json={'state':'sending'})
        assert request.method=='GET' and request.url.path=='/api/inbox/claim/status'
        return httpx.Response(200,json={'state':'succeeded','code':'OK'})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        def tick():worker.tick(client,'https://cloud.invalid',source['ingest_token'],'http://127.0.0.1:18181','K'*48,SESSION,True,True)
        tick();tick();assert not sends
        ready=c.get('/v1/purchases',headers=r).json()['commands'][0]
        if lost=='changed_preview':
            assert ready['state']=='failed';q.close();return
        assert ready['state']=='prepared'
        assert 'reference' not in ready['quote']
        assert c.post('/v1/purchases/'+cmd['id']+'/confirm',headers=o,json={'confirmed':True,'quote_hash':'0'*64}).status_code==409
        assert c.post('/v1/purchases/'+cmd['id']+'/confirm',headers=o,json={'confirmed':True,'quote_hash':ready['quote_hash']}).status_code==200
        try:tick()
        except httpx.ReadTimeout:pass
        q.close();q=Outbox(tmp_path/'inbox.sqlite');worker=PurchaseWorker(q.conn)
        tick();tick();assert len(sends)==1
        assert c.get('/v1/purchases',headers=r).json()['commands'][0]['state']=='succeeded'
    q.close()

@pytest.mark.parametrize('field,value',[('hasActionLink',True),('hasCoupon',True),('hasEntitlement',True),('modCount',1),('attachmentsTruncated',True),('type','NOTICE')])
def test_special_messages_blocked(engine,field,value):
    c,o,w,r,b,source,m=fixture(engine)
    m[field]=value
    with engine.begin() as x:x.execute(db.snapshots.update().where(db.snapshots.c.topic=='inbox').values(payload={'inbox':{'messages':[m]}}))
    assert c.post('/v1/inbox/prepare',headers=o,json=b).status_code==409

@pytest.mark.parametrize('kind',['PARAMETERIZED','GUILDRANKEDREWARD','TERRITORYWARCOMPLETEREWARD'])
def test_unknown_subject_blocked(engine,kind):
    c,o,w,r,b,source,m=fixture(engine)
    m.update(type=kind,subjectKey='UNVERIFIED_SUBJECT')
    with engine.begin() as x:x.execute(db.snapshots.update().where(db.snapshots.c.topic=='inbox').values(payload={'inbox':{'messages':[m]}}))
    assert c.post('/v1/inbox/prepare',headers=o,json=b).status_code==409


def test_tournament_match_reward_exact_subject_and_special_actions():
    from swgoh_cloud.inbox_policy import preview
    m=dict(starts='1',expires='9999999999',attachmentCount=2,attachments=[dict(type='CURRENCY',definition='GRIND',minQuantity=100,maxQuantity=100),dict(type='CURRENCY',definition='PREMIUM',minQuantity=10,maxQuantity=10)]);m.update(type='TERRITORYTOURNAMENTMATCHREWARD',subjectKey='TERRITORYTOURNAMENT_REWARD_NO_CHANGE_FIRST_SUBJECT_KEY')
    assert preview(m)['type']=='TERRITORYTOURNAMENTMATCHREWARD'
    for changes in [{'subjectKey':'OTHER'},{'hasActionLink':True},{'hasEntitlement':True},{'hasCoupon':True},{'modCount':1}]:
        with pytest.raises(ValueError):preview({**m,**changes})
