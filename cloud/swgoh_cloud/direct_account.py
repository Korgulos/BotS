"""Temporary source/account-bound direct reads. No auth renewal or game actions."""
import os,json,hashlib
from datetime import datetime,timedelta,timezone
from typing import Literal
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from . import db
from .models import StrictModel
from .session_wire import exchange

class ReadRequest(StrictModel):
    operation:Literal['Ping','GetInitialData']
    game_closed:Literal[True]

def config(who):
    try:
        s=json.loads(os.environ.get('SWGOH_DIRECT_TEST_SESSION',''))
        expires=datetime.fromisoformat(s['_expiresAt']);captured=datetime.fromisoformat(s['capturedAt'])
        if s['_accountId']!=who['account_id'] or not db.now()<expires<=captured+timedelta(minutes=30):raise ValueError()
        if s['requestTokenMode']!='absent' or s.get('requestToken') or not s['authId'] or not s['authToken'] or not s['expectedAllyCode']:raise ValueError()
        if len(s['_testId'])!=32:raise ValueError()
        return s
    except (ValueError,TypeError,KeyError):raise HTTPException(409,'Privremena direktna sesija nije dostupna ili je istekla')

def install(app,engine,controller):
    @app.post('/v1/direct/account/read')
    def read(data:ReadRequest,who=Depends(controller)):
        s=config(who);session=hashlib.sha256(s['_testId'].encode()).hexdigest();event_key='direct-read:'+data.operation
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            if not c.execute(select(db.sources.c.id).where(db.sources.c.id==s['_sourceId'],db.sources.c.account_id==who['account_id'])).first():raise HTTPException(409,'Izvor direktne sesije nije potvrđen')
            agent=c.execute(select(db.agents).where(db.agents.c.source_id==s['_sourceId'])).mappings().first()
            if agent and agent['online'] and agent['last_seen']>db.now()-timedelta(seconds=30):raise HTTPException(409,'Zatvori igru pre direktnog testa')
            prior=c.execute(select(db.events).where(db.events.c.source_id==s['_sourceId'],db.events.c.session==session,db.events.c.event_key==event_key)).mappings().first()
            if prior:
                if prior['metadata'].get('state')=='succeeded':return prior['payload']
                raise HTTPException(409,'Prethodni pokušaj nije potvrđen; zahtev se ne ponavlja')
            if data.operation=='GetInitialData':
                ping=c.execute(select(db.events.c.payload).where(db.events.c.source_id==s['_sourceId'],db.events.c.session==session,db.events.c.event_key=='direct-read:Ping')).scalar()
                if not ping or not ping.get('success'):raise HTTPException(409,'Prvo potvrdi direktan Ping')
            identifier=db.uid();now=db.now()
            c.execute(db.events.insert().values(id=identifier,account_id=who['account_id'],source_id=s['_sourceId'],session=session,event_key=event_key,revision=1,observed_at=now,received_at=now,service='PlayerRpc',operation=data.operation,topic=None,content_hash=hashlib.sha256(event_key.encode()).hexdigest(),metadata={'state':'sending','source':'direct-session-test'},payload=None))
        # Durable reservation precedes network I/O, including restarts and lost HTTP replies.
        try:
            with httpx.Client(timeout=20,follow_redirects=False,trust_env=False) as client:result=exchange(s,data.operation,client)
            state='succeeded' if result['success'] else 'failed'
        except Exception as error:
            result={'operation':data.operation,'success':False,'outcome':'uncertain','error_type':type(error).__name__,'retry_allowed':False};state='uncertain'
        with engine.begin() as c:
            c.execute(db.events.update().where(db.events.c.id==identifier).values(payload=result,metadata={'state':state,'source':'direct-session-test'},content_hash=hashlib.sha256(json.dumps(result,sort_keys=True).encode()).hexdigest()))
        return result
