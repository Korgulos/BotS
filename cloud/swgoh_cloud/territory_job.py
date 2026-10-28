"""Account-bound read-only TB collection with optional managed EA session renewal."""
import json,os
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from . import db
from .territory import checkpoint
from .territory_wire import collect,SessionRejected

SESSION='territory-schedule-v1';EVENT='hourly'
def session_config(engine=None,account=None):
    if engine is not None and account is not None:
        from .ea_auth import session
        managed=session(engine,account)
        if managed:return managed
    try:
        s=json.loads(os.environ.get('SWGOH_TERRITORY_SESSION',''))
        expiry=datetime.fromisoformat(s['_expiresAt']);captured=datetime.fromisoformat(s['capturedAt'])
        if not captured<=db.now()<expiry<=captured+timedelta(hours=24):raise ValueError()
        if s['requestTokenMode']!='absent' or s.get('requestToken') or not all(s.get(k) for k in ['authId','authToken','expectedAllyCode','_accountId','_sourceId']):raise ValueError()
        return s
    except (ValueError,TypeError,KeyError):raise HTTPException(409,'TB_SESSION_UNAVAILABLE_OR_EXPIRED')

def status(engine,account):
    with engine.connect() as c:
        r=c.execute(select(db.events.c.payload).where(db.events.c.account_id==account,db.events.c.session==SESSION,db.events.c.event_key==EVENT)).scalar()
    return r or {'enabled':False,'interval_seconds':3600,'state':'not_configured'}

def configure(engine,account,enabled):
    if not enabled:
        with engine.begin() as c:
            row=c.execute(select(db.events).where(db.events.c.account_id==account,db.events.c.session==SESSION,db.events.c.event_key==EVENT).with_for_update()).mappings().first()
            if row:
                p=dict(row['payload'],enabled=False,state='disabled');c.execute(db.events.update().where(db.events.c.id==row['id']).values(payload=p));return p
    s=session_config(engine,account)
    if s['_accountId']!=account:raise HTTPException(403,'TB_ACCOUNT_MISMATCH')
    now=db.now()
    with engine.begin() as c:
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==account).with_for_update()).one()
        if not c.execute(select(db.sources.c.id).where(db.sources.c.id==s['_sourceId'],db.sources.c.account_id==account)).first():raise HTTPException(409,'TB_SOURCE_UNVERIFIED')
        row=c.execute(select(db.events).where(db.events.c.account_id==account,db.events.c.session==SESSION,db.events.c.event_key==EVENT)).mappings().first()
        p=dict(row['payload']) if row else {}
        p.update(enabled=enabled,interval_seconds=3600,next_due=p.get('next_due') or now.isoformat(),state='ready' if enabled else 'disabled')
        values=dict(payload=p,content_hash=db.digest(json.dumps(p,sort_keys=True)),received_at=now,observed_at=now)
        if row:c.execute(db.events.update().where(db.events.c.id==row['id']).values(**values))
        else:c.execute(db.events.insert().values(id=db.uid(),account_id=account,source_id=s['_sourceId'],session=SESSION,event_key=EVENT,revision=1,service='Inspector',operation='TerritorySchedule',topic=None,metadata={},**values))
    return p

def run_due(engine,account=None,collector=collect,client_factory=httpx.Client,force=False):
    now=db.now()
    with engine.begin() as c:
        q=select(db.events).where(db.events.c.session==SESSION,db.events.c.event_key==EVENT)
        if account:q=q.where(db.events.c.account_id==account)
        # Each schedule row is locked while its durable reservation is claimed.
        rows=c.execute(q.with_for_update(skip_locked=True)).mappings().all();jobs=[]
        for row in rows:
            p=dict(row['payload']);due=datetime.fromisoformat(p['next_due']);lease=datetime.fromisoformat(p['lease_until']) if p.get('lease_until') else now
            if lease>now:continue
            if force:
                last=datetime.fromisoformat(p['last_attempt']) if p.get('last_attempt') else now-timedelta(hours=1)
                if last>now-timedelta(seconds=60):continue
            elif not p.get('enabled') or due>now:continue
            live=c.execute(select(db.agents.c.source_id).join(db.sources,db.sources.c.id==db.agents.c.source_id).where(db.sources.c.account_id==row['account_id'],db.agents.c.online.is_(True),db.agents.c.last_seen>now-timedelta(seconds=60))).first()
            buying=c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==row['account_id'],db.purchases.c.state.in_(['sending','checking_commit','preparing']))).first()
            if live or buying:
                p.update(state='waiting_for_game_or_transaction',next_due=(now+timedelta(hours=1)).isoformat())
            else:
                p.update(state='running',lease_until=(now+timedelta(minutes=5)).isoformat(),last_attempt=now.isoformat(),next_due=(now+timedelta(hours=1)).isoformat());jobs.append((dict(row),p))
            c.execute(db.events.update().where(db.events.c.id==row['id']).values(payload=p))
    outcomes=[]
    for row,p in jobs:
        snapshot=None
        try:
            s=session_config(engine,row['account_id'])
            if s['_accountId']!=row['account_id'] or s['_sourceId']!=row['source_id']:raise ValueError('Session binding')
            with client_factory(timeout=20,trust_env=False,follow_redirects=False) as client:snapshot=collector(s,client,db.now().isoformat())
            p.update(state='ok',last_success=db.now().isoformat(),last_error=None)
        except SessionRejected:
            from .ea_auth import invalidate
            invalidate(engine,row['account_id'])
            p.update(state='session_rejected',enabled=False,last_error='SESSION_REJECTED')
        except HTTPException:p.update(state='session_unavailable',last_error='SESSION_UNAVAILABLE_OR_EXPIRED')
        except Exception as e:p.update(state='failed',last_error=type(e).__name__) # Never include credential-bearing error text.
        p['lease_until']=None
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==row['account_id']).with_for_update()).one()
            if snapshot:
                checkpoint(c,SimpleNamespace(payload=snapshot,account_id=row['account_id'],source_id=row['source_id']))
            current=c.execute(select(db.events.c.payload).where(db.events.c.id==row['id']).with_for_update()).scalar()
            if current and not current.get('enabled'):p['enabled']=False
            c.execute(db.events.update().where(db.events.c.id==row['id']).values(payload=p,observed_at=db.now(),received_at=db.now(),content_hash=db.digest(json.dumps(p,sort_keys=True))))
        outcomes.append({'state':p['state'],'saved':snapshot is not None})
    return outcomes

def install(app,engine,reader,controller):
    from .models import StrictModel
    class Schedule(StrictModel):enabled:bool
    class Renew(StrictModel):game_closed:bool
    @app.get('/v1/territory/session')
    def session_status(who=Depends(reader)):
        from .ea_auth import status
        return status(engine,who['account_id'])
    @app.post('/v1/territory/session/renew')
    def renew_session(data:Renew,who=Depends(controller)):
        from .ea_auth import session,status
        if not data.game_closed:raise HTTPException(409,'Close the game before renewal')
        if session(engine,who['account_id'],force=True) is None:raise HTTPException(409,'EA_AUTH_NOT_CONFIGURED')
        return status(engine,who['account_id'])
    @app.get('/v1/territory/collection')
    def get_status(who=Depends(reader)):return status(engine,who['account_id'])
    @app.post('/v1/territory/collection')
    def set_schedule(data:Schedule,who=Depends(controller)):return configure(engine,who['account_id'],data.enabled)
    @app.post('/v1/territory/collect')
    def collect_now(who=Depends(controller)):
        if status(engine,who['account_id'])['state']=='not_configured':configure(engine,who['account_id'],False)
        results=run_due(engine,who['account_id'],force=True)
        if not results:raise HTTPException(409,'TB očitavanje je već u toku, igra je otvorena ili nije prošao minut od prethodnog pokušaja')
        return {'results':results,'status':status(engine,who['account_id'])}

def main():
    from .config import Settings
    settings=Settings.from_env();engine=db.engine_for(settings.database_url)
    try:print(json.dumps({'territory_collection':run_due(engine)}))
    finally:engine.dispose()
if __name__=='__main__':main()
