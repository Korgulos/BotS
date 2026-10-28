"""Read-only server shipment refresh. Never pays for a shop refresh."""
import copy
from datetime import timedelta,datetime
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from . import db
from .shipment_wire import fields,child,one,bucket,mod_eligible,receive
from .session_wire import build_request,initial_projection
from .currency_policy import ITEM_TYPES
from .direct_purchases import SHOPS,session,offline,session_id
from .models import Observation,IngestBatch
from .store import ingest

TYPES={v:k for k,v in ITEM_TYPES.items()}
def projection(payload,metadata=None):
    metadata=metadata or {};store=child(fields(payload),18);boards=[];now=int(db.now().timestamp())
    raw_boards=store.get(10,[])
    if len(raw_boards)>32:raise ValueError('Too many shipment boards')
    for blob in raw_boards:
        board=fields(blob);kind=one(board,1,2)
        if kind not in SHOPS:continue
        refresh=one(board,3,0);items=[];raw_items=board.get(2,[])
        if len(raw_items)>500:raise ValueError('Too many shipment offers')
        for raw in raw_items:
            row=fields(raw)
            if one(row,18,b'') or one(row,15,0)>now or 0<one(row,16,0)<=now:continue
            reward=bucket(one(row,13,b''));cost=bucket(one(row,14,b''));identifier=one(row,12,b'').decode()
            if not identifier or any(x['type'] not in TYPES or not x['definition'] or not 0<x['min']<=x['max']<=2147483647 for x in (reward,cost)):continue
            def item(x):return dict(definition=x['definition'],type=TYPES[x['type']],minQuantity=x['min'],maxQuantity=x['max'])
            r=item(reward)
            if mod_eligible(reward,metadata):r['mod']=copy.deepcopy(metadata[reward['definition']])
            items.append(dict(itemId=identifier,purchased=one(row,17,0)>0,reward=r,cost=item(cost)))
        boards.append(dict(type=SHOPS[kind],nextRefresh=str(refresh),items=items))
    if not boards:raise ValueError('No verified shipment boards')
    return dict(available=True,observedAt=db.now().isoformat(),offers=dict(kind='shipments',shipments=boards))

def refresh(engine,who,client_factory=httpx.Client):
    from .ea_auth import session as managed_session
    from .purchases import ACTIVE
    managed_session(engine,who['account_id'])
    with engine.begin() as c:
        c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
        s=session(c,who);offline(c,who['account_id'])
        if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():raise HTTPException(409,'Sačekaj završetak prethodne akcije')
        prior=c.execute(select(db.events).where(db.events.c.account_id==who['account_id'],db.events.c.session==session_id(s),db.events.c.event_key=='shipment-refresh')).mappings().first()
        if prior and prior['received_at']>db.now()-timedelta(seconds=15):raise HTTPException(429,'Sačekaj 15 sekundi između očitavanja ponuda')
        values=dict(observed_at=db.now(),received_at=db.now(),metadata={'state':'reading'},payload=None,content_hash='shipment-refresh')
        if prior:c.execute(db.events.update().where(db.events.c.id==prior['id']).values(**values))
        else:c.execute(db.events.insert().values(id=db.uid(),account_id=who['account_id'],source_id=s['_sourceId'],session=session_id(s),event_key='shipment-refresh',revision=1,service='PlayerRpc',operation='GetInitialData',topic=None,**values))
        rewards=c.execute(select(db.offers.c.reward).join(db.snapshots,db.snapshots.c.event_id==db.offers.c.event_id).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.source_id==s['_sourceId'],db.snapshots.c.topic=='shipments')).scalars().all()
        metadata={r['definition']:r['mod'] for r in rewards if r and r.get('mod') and r.get('definition')}
    try:
        with client_factory(timeout=25,trust_env=False,follow_redirects=False) as client:code,payload=receive(client,build_request(s,'GetInitialData'))
        if code!=1:
            from .ea_auth import invalidate
            invalidate(engine,who['account_id']);raise ValueError('Session rejected')
        if str(initial_projection(payload)['player']['allyCode'])!=str(s['expectedAllyCode']):raise ValueError('Account mismatch')
        document=projection(payload,metadata);now=db.now()
        event=Observation(session=session_id(s),event_key='shipments:'+db.uid(),revision=1,observed_at=now,service='PlayerRpc',operation='GetInitialData',topic='shipments',payload=document)
        with engine.begin() as c:
            ingest(c,{'account_id':who['account_id'],'source_id':s['_sourceId']},IngestBatch(events=[event]))
        from .inventory import projection as inventory_projection,save
        try:save(engine,{'account_id':who['account_id'],'source_id':s['_sourceId'],'session':session_id(s)},inventory_projection(payload))
        except Exception:pass # Shipment refresh is already committed; inventory is best effort.
        return {'state':'ok','offers':sum(len(b['items']) for b in document['offers']['shipments'])}
    except Exception:raise HTTPException(409,'Ponude nisu očitane. Sačuvani podaci su zadržani; proveri EA prijavu.')

def install(app,engine,controller):
    @app.post('/v1/shipments/refresh')
    def refresh_shipments(who=Depends(controller)):return refresh(engine,who)
