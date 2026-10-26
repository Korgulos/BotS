"""Explicit opt-in reward-only inbox collection through the existing game bridge."""
import json,hashlib
from datetime import timedelta
from uuid import UUID,uuid5,NAMESPACE_URL
from fastapi import Depends,HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import db
from .models import StrictModel
from .inbox_policy import preview,message_hash
POLICY_SESSION='inbox-auto-policy'
class Policy(StrictModel):
    enabled:bool
class Tick(StrictModel):
    session:str=Field(pattern=r'^[a-f0-9]{64}$')

def settings(c,account,source=None):
    query=select(db.events).where(db.events.c.account_id==account,db.events.c.session==POLICY_SESSION,db.events.c.event_key=='inbox-auto-policy')
    if source:query=query.where(db.events.c.source_id==source)
    row=c.execute(query.order_by(db.events.c.received_at.desc()).limit(1)).mappings().first()
    return {'enabled':bool(row and (row['payload'] or {}).get('enabled')),'source_id':row['source_id'] if row else None,'requires_game':True}

def automatic_key(message):
    # Local numbering and opaque handles can change when earlier messages disappear.
    return hashlib.sha256(json.dumps(preview(message),sort_keys=True,separators=(',',':')).encode()).hexdigest()

def install(app,engine,reader,controller,writer):
    @app.get('/v1/inbox/automation')
    def read(who=Depends(reader)):
        with engine.connect() as c:return settings(c,who['account_id'])
    @app.post('/v1/inbox/automation')
    def set_policy(data:Policy,who=Depends(controller)):
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            snap=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.topic=='inbox').order_by(db.snapshots.c.observed_at.desc()).limit(1)).mappings().first()
            if not snap:raise HTTPException(409,'Prvo poveži inbox snimak iz igre')
            now=db.now();payload={'enabled':data.enabled,'supported_rewards_only':True};values=dict(account_id=who['account_id'],source_id=snap['source_id'],session=POLICY_SESSION,event_key='inbox-auto-policy',revision=1,observed_at=now,received_at=now,service='Inspector',operation='InboxAutoPolicy',topic=None,metadata={'source':'control-policy'},payload=payload,content_hash=hashlib.sha256(json.dumps(payload).encode()).hexdigest())
            c.execute(db.events.update().where(db.events.c.account_id==who['account_id'],db.events.c.session==POLICY_SESSION,db.events.c.event_key=='inbox-auto-policy').values(payload={'enabled':False,'supported_rewards_only':True}))
            q=insert(db.events).values(id=db.uid(),**values)
            c.execute(q.on_conflict_do_update(index_elements=['source_id','session','event_key'],set_=values))
            if not data.enabled:
                rows=c.execute(select(db.purchases).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.expected['automatic'].as_boolean().is_(True),db.purchases.c.state.in_(['queued_prepare','preparing','prepared','queued_commit']))).mappings().all()
                for row in rows:c.execute(db.purchases.update().where(db.purchases.c.id==row['id'],db.purchases.c.state.in_(['queued_prepare','preparing','prepared','queued_commit'])).values(state='cancelled',code='AUTOMATION_DISABLED',updated_at=now))
            return settings(c,who['account_id'])
    @app.post('/v1/bridge/inbox/auto-prepare')
    def tick(data:Tick,who=Depends(writer)):
        from .purchases import ACTIVE
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            if not settings(c,who['account_id'],who['source_id'])['enabled']:return {'queued':False}
            agent=c.execute(select(db.agents).where(db.agents.c.source_id==who['source_id'])).mappings().first()
            if not agent or agent['session']!=data.session or not agent['online'] or not agent['purchases_enabled'] or agent['last_seen']<db.now()-timedelta(seconds=20):return {'queued':False}
            if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():return {'queued':False}
            snap=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.source_id==who['source_id'],db.snapshots.c.topic=='inbox')).mappings().first()
            if not snap:return {'queued':False}
            messages=snap['payload'].get('inbox',{}).get('messages',[])
            candidates=[];counts={}
            for ordinal,m in enumerate(messages[:100]):
                try:
                    p=preview(m)
                    if not int(p['starts'])<=db.now().timestamp()<int(p['expires']):continue
                    key=automatic_key(m);candidates.append((ordinal,m,p,key));counts[key]=counts.get(key,0)+1
                except (ValueError,TypeError,KeyError):continue
            for ordinal,m,p,key in candidates:
                if counts[key]!=1:continue # Identical visible messages need manual identification.
                request_id=str(uuid5(NAMESPACE_URL,who['account_id']+':inbox-auto:'+key))
                if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.request_id==request_id)).first():continue
                # A manual unresolved send with the same visible reward also blocks automation.
                prior=c.execute(select(db.purchases.c.expected).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.expected['kind'].as_string()=='inbox')).scalars().all()
                if any(x.get('preview')==p for x in prior):continue
                now=db.now();identifier=db.uid()
                c.execute(db.purchases.insert().values(id=identifier,account_id=who['account_id'],source_id=who['source_id'],session=data.session,request_id=request_id,event_id=snap['event_id'],ordinal=ordinal,state='queued_prepare',expected={'kind':'inbox','automatic':True,'preview':p,'message_hash':message_hash(m)},quote=None,quote_hash=None,code='AUTO_INBOX_QUEUED',created_at=now,updated_at=now,expires_at=now+timedelta(seconds=45),confirmed_at=None))
                return {'queued':True,'id':identifier}
            return {'queued':False}
