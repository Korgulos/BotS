"""Durable opt-in free-reward collection, invoked by the user's local timer."""
import hashlib,json
from datetime import timedelta
from uuid import UUID
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from . import db
from .models import StrictModel
from .territory_job import session_config,configure,run_due,status as tb_status
from .reward_wire import Rewards
from .shipment_wire import receive
from .inbox_policy import preview

SESSION='direct-maintenance-v1'
class Run(StrictModel):request_id:UUID

def digest(value):return db.digest(json.dumps(value,sort_keys=True,separators=(',',':')))
def journal(engine,s,key,operation,payload):
    with engine.begin() as c:
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==s['_accountId']).with_for_update()).one()
        old=c.execute(select(db.events).where(db.events.c.account_id==s['_accountId'],db.events.c.session==SESSION,db.events.c.event_key==key).with_for_update()).mappings().first()
        if old:return None,old['payload']
        identifier=db.uid();now=db.now()
        c.execute(db.events.insert().values(id=identifier,account_id=s['_accountId'],source_id=s['_sourceId'],session=SESSION,event_key=key,revision=1,observed_at=now,received_at=now,service='PlayerRpc',operation=operation,topic=None,metadata={'source':'local-hourly-timer'},payload=payload,content_hash=digest(payload)))
        return identifier,None

def settle(engine,identifier,payload):
    with engine.begin() as c:c.execute(db.events.update().where(db.events.c.id==identifier).values(payload=payload,received_at=db.now(),content_hash=digest(payload)))

def claim(engine,s,client,codec,operation,key,document,message=None):
    # Build and validate before reserving; after reservation a transport failure never re-sends.
    packet=codec.packet(s,operation,message)
    identifier,old=journal(engine,s,key,operation,{'state':'sending',**document})
    if not identifier:return {'state':'already_processed' if old.get('state')=='ok' else 'uncertain' if old.get('state') in {'sending','uncertain'} else 'already_attempted'}
    try:
        code,_=receive(client,packet);result={'state':'ok' if code==1 else 'rejected','rpc_code':code,**document}
    except Exception as e:result={'state':'uncertain','error_type':type(e).__name__,**document}
    settle(engine,identifier,result);return {k:result[k] for k in ('state','rpc_code') if k in result}

def rewards(engine,s,client,codec=None):
    codec=codec or Rewards();initial=codec.initial(s,client);now=int(db.now().timestamp());messages=codec.inbox(initial)
    inbox={'claimed':0,'skipped':0,'uncertain':0};attempts=0
    # Reconcile only absence of a particular inbox ID; never infer from attachment similarity.
    live_keys={'inbox:'+hashlib.sha256(identifier.encode()).hexdigest() for identifier,_,_ in messages}
    with engine.begin() as c:
        pending=c.execute(select(db.events).where(db.events.c.account_id==s['_accountId'],db.events.c.session==SESSION,db.events.c.operation=='UpdateInbox').with_for_update()).mappings().all()
        for row in pending:
            if row['event_key'] not in live_keys and row['payload'].get('state') in {'sending','uncertain'}:
                value=dict(row['payload'],state='reconciled_absent');c.execute(db.events.update().where(db.events.c.id==row['id']).values(payload=value,content_hash=digest(value)))
    for message in messages:
        identifier,kind,doc=message
        try:
            p=preview(doc)
            if not int(p['starts'])<=now<int(p['expires']):raise ValueError()
        except (ValueError,TypeError,KeyError):inbox['skipped']+=1;continue
        if attempts>=5:inbox['skipped']+=1;continue
        key='inbox:'+hashlib.sha256(identifier.encode()).hexdigest()
        result=claim(engine,s,client,codec,'UpdateInbox',key,{'preview':p},message)
        if result['state'] in {'ok','rejected','uncertain'}:attempts+=1
        if result['state']=='ok':inbox['claimed']+=1
        elif result['state']=='uncertain':inbox['uncertain']+=1
        else:inbox['skipped']+=1
        if result['state']=='uncertain':break
    before=codec.bonus_times(initial);signature=digest(before)
    with engine.begin() as c:
        pending=c.execute(select(db.events).where(db.events.c.account_id==s['_accountId'],db.events.c.session==SESSION,db.events.c.operation=='ClaimAllEnergyRewards').with_for_update()).mappings().all();blocked=False
        for row in pending:
            if row['payload'].get('state') not in {'sending','uncertain'}:continue
            if row['payload'].get('before')==signature:blocked=True
            else:
                value=dict(row['payload'],state='reconciled_bonus_changed');c.execute(db.events.update().where(db.events.c.id==row['id']).values(payload=value,content_hash=digest(value)))
    if blocked or inbox['uncertain']:energy={'state':'uncertain','sent':False}
    else:
        hour=db.now().strftime('%Y%m%d%H')
        energy=claim(engine,s,client,codec,'ClaimAllEnergyRewards','energy:'+hour,{'before':signature})
    # Refresh even after an uncertain send: readback is safe; never resend the mutation.
    fresh=codec.initial(s,client)
    from .inventory import projection,save
    save(engine,{'account_id':s['_accountId'],'source_id':s['_sourceId'],'session':SESSION},projection(fresh),transport='direct-maintenance')
    new_messages=codec.inbox(fresh)
    public_inbox={'inbox':{'messages':[m[2] for m in new_messages]}}
    public_energy={'source':'server-Player-energy','bonusTimes':codec.bonus_times(fresh),'rewards':[],'availabilityKnown':False,'lastClaim':energy}
    from .store import ingest
    from .models import Observation,IngestBatch
    now=db.now()
    events=[Observation(session=SESSION,event_key='refresh:'+topic+':'+db.uid(),revision=1,observed_at=now,service='PlayerRpc',operation='GetInitialData',topic=topic,payload=p) for topic,p in [('inbox',public_inbox),('energy',public_energy)]]
    with engine.begin() as c:ingest(c,{'account_id':s['_accountId'],'source_id':s['_sourceId']},IngestBatch(events=events))
    energy['bonus_changed']=codec.bonus_times(fresh)!=before
    return dict(inbox=inbox,energy=energy,refreshed=True,state='uncertain' if inbox['uncertain'] or energy['state']=='uncertain' else 'ok')

def run(engine,who,request_id,collector=rewards):
    try:s=session_config(engine,who['account_id'])
    except HTTPException:return {'state':'session_unavailable','code':'SESSION_EXPIRED_OR_MISSING'}
    if s['_accountId']!=who['account_id']:raise HTTPException(403,'Account mismatch')
    account=s['_accountId'];now=db.now();event_key='run:'+str(request_id)
    with engine.begin() as c:
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==account).with_for_update()).one()
        if not c.execute(select(db.sources.c.id).where(db.sources.c.id==s['_sourceId'],db.sources.c.account_id==account)).first():raise HTTPException(409,'Source mismatch')
        prior=c.execute(select(db.events).where(db.events.c.account_id==account,db.events.c.session==SESSION,db.events.c.event_key==event_key)).mappings().first()
        if prior:return prior['payload']
        recent=c.execute(select(db.events).where(db.events.c.account_id==account,db.events.c.session==SESSION,db.events.c.operation=='MaintenanceRun').order_by(db.events.c.received_at.desc()).limit(1)).mappings().first()
        if recent and recent['payload'].get('state')=='running' and recent['received_at']>now-timedelta(minutes=10):return {'state':'already_running'}
        online=c.execute(select(db.agents.c.source_id).join(db.sources,db.sources.c.id==db.agents.c.source_id).where(db.sources.c.account_id==account,db.agents.c.online.is_(True),db.agents.c.last_seen>now-timedelta(seconds=60))).first()
        active=c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==account,db.purchases.c.state.in_(['sending','checking_commit','preparing','queued_commit']))).first()
        if online or active:return {'state':'game_open_or_transaction_active'}
        value={'state':'running'};identifier=db.uid()
        c.execute(db.events.insert().values(id=identifier,account_id=account,source_id=s['_sourceId'],session=SESSION,event_key=event_key,revision=1,observed_at=now,received_at=now,service='Inspector',operation='MaintenanceRun',topic=None,metadata={'source':'local-hourly-timer'},payload=value,content_hash=digest(value)))
    try:
        if tb_status(engine,account)['state']=='not_configured':configure(engine,account,False)
        tb=run_due(engine,account,force=True)
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:result=collector(engine,s,client)
        result['tb']=tb
    except Exception as e:result={'state':'failed','code':type(e).__name__}
    settle(engine,identifier,result);return result

def install(app,engine,reader,controller):
    @app.post('/v1/maintenance/run')
    def maintenance(data:Run,who=Depends(controller)):return run(engine,who,data.request_id)
    @app.get('/v1/maintenance/status')
    def status(who=Depends(reader)):
        with engine.connect() as c:
            row=c.execute(select(db.events.c.payload,db.events.c.received_at).where(db.events.c.account_id==who['account_id'],db.events.c.session==SESSION,db.events.c.operation=='MaintenanceRun').order_by(db.events.c.received_at.desc()).limit(1)).mappings().first()
            return dict(row) if row else {'payload':{'state':'not_run'}}
