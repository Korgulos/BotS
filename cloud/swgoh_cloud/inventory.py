"""Definition-level inventory only: no player, unit instance, mod instance or auth IDs."""
from datetime import timedelta
import re,json,hashlib
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import db
from .shipment_wire import fields,one,child,receive
from .currency_policy import CURRENCY_IDS
from .session_wire import build_request,initial_projection

TYPES=['CURRENCY','MATERIAL','EQUIPMENT','UNIT','STATMOD']
CURRENCIES={v:k for k,v in CURRENCY_IDS.items()}
def canonical(kind,definition):
    if kind=='STATMOD':return definition.split(':STATMOD_')[0]
    if kind=='UNIT':return definition.split(':')[0]
    return definition

def projection(payload):
    root=fields(payload)
    if 5 not in root:raise ValueError('Inventory section missing')
    inventory=child(root,5);rows={}
    def add(kind,definition,quantity,bonus=0,aggregate=False):
        if not isinstance(definition,str) or not re.fullmatch(r'[A-Za-z0-9_:./+\-]{1,160}',definition):raise ValueError('Invalid inventory definition')
        definition=canonical(kind,definition)
        if type(quantity) is not int or type(bonus) is not int or not 0<=quantity<2**63 or not 0<=bonus<2**31:raise ValueError('Invalid inventory quantity')
        key=(kind,definition)
        if key in rows and not aggregate:raise ValueError('Duplicate inventory resource')
        old=rows.get(key,{'quantity':'0','bonus':'0'})
        quantity+=int(old['quantity']);bonus+=int(old['bonus'])
        if quantity>=2**63:raise ValueError('Inventory quantity overflow')
        rows[key]={'type':kind,'definition':definition,'quantity':str(quantity),'bonus':str(bonus)}
    for blob in inventory.get(9,[]):
        n=fields(blob);currency=one(n,1,1);add('CURRENCY',CURRENCIES.get(currency,'CURRENCY_'+str(currency)),one(n,2,0),one(n,3,0))
    for tag,kind in [(5,'MATERIAL'),(10,'EQUIPMENT')]:
        for blob in inventory.get(tag,[]):
            n=fields(blob);add(kind,one(n,1,b'').decode('utf8'),one(n,2,0),one(n,3,0))
    mods=list(inventory.get(13,[]))
    for blob in inventory.get(1,[]):
        n=fields(blob);add('UNIT',one(n,2,b'').decode('utf8'),1,aggregate=True);mods.extend(n.get(21,[]))
    for blob in mods:
        n=fields(blob);add('STATMOD',one(n,2,b'').decode('utf8'),1,aggregate=True)
    if len(rows)>5000:raise ValueError('Inventory exceeds item limit')
    return {'available':True,'completeTypes':TYPES,'items':sorted(rows.values(),key=lambda x:(x['type'],x['definition'])),'needsRefresh':False,'modScope':'equipped-and-unequipped'}

def save(engine,identity,document,transport='direct'):
    now=db.now();document={**document,'observedAt':now.isoformat(),'transport':transport}
    identifier=db.uid();event_key='inventory:'+identifier
    with engine.begin() as c:
        c.execute(db.events.insert().values(id=identifier,account_id=identity['account_id'],source_id=identity['source_id'],session=identity['session'],event_key=event_key,revision=1,observed_at=now,received_at=now,service='PlayerRpc',operation='GetInitialData',topic='inventory',metadata={'source':transport,'state':'succeeded'},payload=document,content_hash=hashlib.sha256(json.dumps(document,sort_keys=True).encode()).hexdigest()))
        query=insert(db.snapshots).values(account_id=identity['account_id'],source_id=identity['source_id'],topic='inventory',event_id=identifier,observed_at=now,payload=document)
        c.execute(query.on_conflict_do_update(index_elements=['account_id','source_id','topic'],set_={'event_id':identifier,'observed_at':now,'payload':document},where=db.snapshots.c.observed_at<=now))
    return document

def stale(engine,account,source):
    with engine.begin() as c:
        snap=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==account,db.snapshots.c.source_id==source,db.snapshots.c.topic=='inventory').with_for_update()).mappings().first()
        if snap:c.execute(db.snapshots.update().where(db.snapshots.c.account_id==account,db.snapshots.c.source_id==source,db.snapshots.c.topic=='inventory').values(payload={**snap['payload'],'needsRefresh':True}))

def read_and_save(engine,s):
    with httpx.Client(timeout=25,trust_env=False,follow_redirects=False) as client:code,payload=receive(client,build_request(s,'GetInitialData'))
    if code!=1:raise ValueError('Session rejected')
    if initial_projection(payload)['player']['allyCode']!=s['expectedAllyCode']:raise ValueError('Account mismatch')
    return save(engine,{'account_id':s['_accountId'],'source_id':s['_sourceId'],'session':hashlib.sha256(s['_testId'].encode()).hexdigest()},projection(payload))

def install(app,engine,reader,controller):
    @app.get('/v1/inventory')
    def inventory(who=Depends(reader)):
        with engine.connect() as c:
            row=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.topic=='inventory').order_by(db.snapshots.c.observed_at.desc()).limit(1)).mappings().first()
            return dict(row) if row else None
    @app.post('/v1/inventory/refresh')
    def refresh(who=Depends(controller)):
        from .direct_purchases import session,offline
        from .purchases import ACTIVE
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            s=session(c,who);offline(c,who['account_id'])
            if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():raise HTTPException(409,'Sačekaj završetak kupovine pre osvežavanja inventara')
            prior=c.execute(select(db.events).where(db.events.c.account_id==who['account_id'],db.events.c.event_key=='inventory-refresh',db.events.c.session==s['_testId'])).mappings().first()
            if prior and prior['received_at']>db.now()-timedelta(seconds=15):raise HTTPException(429,'Sačekaj 15 sekundi između očitavanja inventara')
            values=dict(observed_at=db.now(),received_at=db.now(),metadata={'state':'reading'},payload=None,content_hash='inventory-refresh')
            if prior:c.execute(db.events.update().where(db.events.c.id==prior['id']).values(**values))
            else:c.execute(db.events.insert().values(id=db.uid(),account_id=who['account_id'],source_id=s['_sourceId'],session=s['_testId'],event_key='inventory-refresh',revision=1,service='PlayerRpc',operation='GetInitialData',topic=None,**values))
        try:return read_and_save(engine,s)
        except Exception:raise HTTPException(409,'Inventar nije očitan; proveri direktnu sesiju. Sačuvani snimak je zadržan.')
