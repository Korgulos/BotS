"""Explicit direct shipment purchases using a bounded Render secret and durable journal."""
from datetime import timedelta,datetime
import copy,hashlib
import httpx
from fastapi import Depends,HTTPException
from sqlalchemy import select
from . import db
from .direct_account import config
from .currency_policy import ALLOWED,CURRENCY_IDS,ITEM_TYPES
from .mod_policy import is_mod,eligible_mod
from .purchases import Prepare,Confirm,ACTIVE,fingerprint
from .session_wire import build_request,initial_projection
from .shipment_wire import offers,purchase_packet,receive,fields,one,child

SHOPS={1:'SHIPMENTPVP',2:'SHIPMENTPVE',3:'SHIPMENTSHIPWAR',4:'SHIPMENTCANTINA',5:'SHIPMENTSHIPPVP',6:'SHIPMENTSHARD',7:'SHIPMENTGUILD',8:'SHIPMENTMOD',9:'SHIPMENTTERRITORY',10:'SHIPMENTPREMIUM',11:'SHIPMENTSEASONS',12:'SHIPMENTCONQUEST',13:'SHIPMENTDATACRON',14:'SHIPMENTGALACTICLEGENDS',15:'SHIPMENTEPISODE',16:'SHIPMENTERA'}

def session_id(s):return hashlib.sha256(('direct-purchase:'+s['_testId']).encode()).hexdigest()
def offline(c,account):
    live=c.execute(select(db.agents.c.source_id).join(db.sources,db.sources.c.id==db.agents.c.source_id).where(db.sources.c.account_id==account,db.agents.c.online.is_(True),db.agents.c.last_seen>db.now()-timedelta(seconds=30))).first()
    if live:raise HTTPException(409,'Direktna kupovina zahteva ugašenu igru; sačekaj prekid bridge veze')
def session(c,who):
    s=config(who)
    if s.get('_allowPurchases') is not True:raise HTTPException(409,'Direktna sesija nema odobren pristup kupovinama')
    if not c.execute(select(db.sources.c.id).where(db.sources.c.id==s['_sourceId'],db.sources.c.account_id==who['account_id'])).first():raise HTTPException(409,'Izvor direktne sesije nije potvrđen')
    invalid=c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.session==session_id(s),db.purchases.c.code=='DIRECT_SESSION_REJECTED')).first()
    if invalid:raise HTTPException(409,'Server je odbio sesiju; potrebna je nova prijava kroz igru')
    return s

def status(c,who):
    try:
        s=session(c,who);offline(c,who['account_id'])
        return {'available':True,'source_id':s['_sourceId'],'expires_at':s['_expiresAt'],'currencies':sorted(ALLOWED),'reason':'Direktno · bez kristala · igra ugašena'}
    except HTTPException as e:return {'available':False,'reason':e.detail}

class DirectPurchases:
    def __init__(self,engine):self.engine=engine
    def read(self,s):
        with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:code,payload=receive(client,build_request(s,'GetInitialData'))
        if code!=1:raise SessionRejected()
        if initial_projection(payload)['player']['allyCode']!=s['expectedAllyCode']:raise ValueError('Account mismatch')
        with self.engine.connect() as c:
            rewards=c.execute(select(db.offers.c.reward).join(db.snapshots,db.snapshots.c.event_id==db.offers.c.event_id).where(db.snapshots.c.account_id==s['_accountId'],db.snapshots.c.source_id==s['_sourceId'],db.snapshots.c.topic=='shipments')).scalars().all()
        metadata={r['definition']:r['mod'] for r in rewards if r and eligible_mod(r) and r.get('definition')}
        return offers(payload,mod_metadata=metadata)
    def quote(self,row,rows):
        expected=row['expected']
        matches=[q for q in rows if SHOPS.get(q['shipment_type'])==expected['shop'] and q['item_id']==expected['itemId']]
        if len(matches)!=1:raise ValueError('Offer unavailable')
        fresh=matches[0]
        q=dict(shop=expected['shop'],itemId=fresh['item_id'],reward=fresh['definition'],quantity=fresh['quantity'],currency=fresh['currency'],price=fresh['price'],balance=fresh['balance'],balanceAfter=fresh['balance']-fresh['price'])
        if any(q[k]!=expected[k] for k in ('shop','itemId','reward','quantity','currency','price')) or str(fresh['refresh'])!=expected['refresh']:raise ValueError('Offer changed')
        return q,fresh
    def own(self,c,identifier,who):
        row=c.execute(select(db.purchases).where(db.purchases.c.id==identifier,db.purchases.c.account_id==who['account_id']).with_for_update()).mappings().first()
        if not row or row['expected'].get('transport')!='direct':raise HTTPException(404,'Direktna kupovina nije pronađena')
        return dict(row)
    def update(self,c,row,**values):
        values['updated_at']=db.now();c.execute(db.purchases.update().where(db.purchases.c.id==row['id']).values(**values));row.update(values);return row
    def finish(self,identifier,who,state,code):
        with self.engine.begin() as c:
            row=self.own(c,identifier,who)
            if row['state'] in {'succeeded','failed','cancelled','expired'}:return row
            return self.update(c,row,state=state,code=code)
    def prepare(self,data,who):
        with self.engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            prior=c.execute(select(db.purchases).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.request_id==str(data.request_id))).mappings().first()
            if prior:
                if prior['event_id']!=str(data.event_id) or prior['ordinal']!=data.ordinal or prior['expected'].get('transport')!='direct':raise HTTPException(409,'ID zahteva je već upotrebljen')
                return dict(prior)
            c.execute(db.purchases.update().where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(['queued_prepare','prepared','queued_commit']),db.purchases.c.expires_at<db.now()).values(state='expired',code='APPROVAL_EXPIRED',updated_at=db.now()))
            if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():raise HTTPException(409,'Prethodna akcija nije završena; ne ponavljaj zahtev')
            s=session(c,who);offline(c,who['account_id'])
            offer=c.execute(select(db.offers).join(db.snapshots,db.snapshots.c.event_id==db.offers.c.event_id).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.source_id==s['_sourceId'],db.snapshots.c.topic=='shipments',db.offers.c.event_id==str(data.event_id),db.offers.c.ordinal==data.ordinal)).mappings().first()
            if not offer:raise HTTPException(409,'Potrebna je trenutna lista ponuda')
            price=offer['price'] or {};reward=offer['reward'] or {};cost=price.get('minQuantity');quantity=reward.get('minQuantity')
            if price.get('type')!='CURRENCY' or price.get('definition') not in ALLOWED or str(reward.get('type','')).replace('_','') not in {name.replace('_','') for name in ITEM_TYPES if name!='EMPTY'} or offer['purchased'] is not False or type(cost) is not int or type(quantity) is not int or not 0<cost<=2147483647 or not 0<quantity<=2147483647 or cost!=price.get('maxQuantity') or quantity!=reward.get('maxQuantity') or not offer['definition']:raise HTTPException(409,'Ponuda ili valuta nisu potvrđeni; kristali i novac su blokirani')
            if is_mod(offer['shop'],reward) and not eligible_mod(reward):raise HTTPException(409,'Dozvoljeni su samo potvrđeni modovi sa 5★ i Speed sekundarnim statom')
            expected=dict(shop=offer['shop'],itemId=offer['offer_id'],reward=offer['definition'],quantity=quantity,currency=price['definition'],price=cost,refresh=offer['refresh'],transport='direct')
            row=dict(id=db.uid(),account_id=who['account_id'],source_id=s['_sourceId'],session=session_id(s),request_id=str(data.request_id),event_id=str(data.event_id),ordinal=data.ordinal,state='preparing',expected=expected,quote=None,quote_hash=None,code='DIRECT_CHECKING',created_at=db.now(),updated_at=db.now(),expires_at=db.now()+timedelta(seconds=60),confirmed_at=None)
            c.execute(db.purchases.insert().values(**row))
        try:
            q,_=self.quote(row,self.read(s))
            with self.engine.begin() as c:
                row=self.own(c,row['id'],who);current=session(c,who);offline(c,who['account_id'])
                if row['state']!='preparing':return row
                if session_id(current)!=row['session'] or row['expires_at']<=db.now():return self.update(c,row,state='expired',code='APPROVAL_EXPIRED')
                return self.update(c,row,state='prepared',quote=q,quote_hash=fingerprint(q),code='DIRECT_READY',expires_at=min(db.now()+timedelta(seconds=30),datetime.fromisoformat(s['_expiresAt'])))
        except Exception as error:return self.finish(row['id'],who,'failed','DIRECT_SESSION_REJECTED' if isinstance(error,SessionRejected) else 'DIRECT_CHECK_FAILED')
    def confirm(self,identifier,data,who):
        with self.engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            row=self.own(c,identifier,who)
            if data.quote_hash!=row['quote_hash']:raise HTTPException(409,'Ponuda nije važeća')
            if row['confirmed_at'] is not None:return row
            if row['state']!='prepared' or row['expires_at']<=db.now():raise HTTPException(409,'Potvrda je istekla ili nije spremna')
            s=session(c,who);offline(c,who['account_id'])
            if session_id(s)!=row['session']:raise HTTPException(409,'Direktna sesija je promenjena; pripremi novu ponudu')
            self.update(c,row,state='checking_commit',confirmed_at=db.now(),code='DIRECT_RECHECK')
        sending=False
        try:
            q,fresh=self.quote(row,self.read(s))
            with self.engine.begin() as c:
                row=self.own(c,identifier,who);current=session(c,who);offline(c,who['account_id'])
                if row['state']!='checking_commit' or row['expires_at']<=db.now() or session_id(current)!=row['session']:raise ValueError('Approval changed')
                self.update(c,row,state='sending',code='DIRECT_SENDING')
            sending=True
            with httpx.Client(timeout=20,trust_env=False,follow_redirects=False) as client:code,payload=receive(client,purchase_packet(s,fresh))
            if code!=1:return self.finish(identifier,who,'failed','DIRECT_RPC_REJECTED')
            delta=child(fields(payload),1);debit=child(delta,2);costs=[]
            for blob in debit.get(1,[]):
                n=fields(blob);costs.append((one(n,1,1),one(n,2,0)))
            if costs!=[(CURRENCY_IDS[q['currency']],q['price'])]:raise ValueError('Debit mismatch')
            with self.engine.begin() as c:
                row=self.own(c,identifier,who)
                self.update(c,row,state='succeeded',code='DIRECT_OK')
                # Keep SQL and the visible saved snapshot consistent, without reintroducing old offers.
                c.execute(db.offers.update().where(db.offers.c.event_id==row['event_id'],db.offers.c.ordinal==row['ordinal']).values(purchased=True))
                snap=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.source_id==row['source_id'],db.snapshots.c.topic=='shipments').with_for_update()).mappings().first()
                if snap:
                    payload=copy.deepcopy(snap['payload'])
                    for board in payload.get('offers',{}).get('shipments',[]):
                        if board.get('type')==q['shop'] and str(board.get('nextRefresh'))==row['expected']['refresh']:
                            for item in board.get('items',[]):
                                if item.get('itemId')==q['itemId']:item['purchased']=True
                    c.execute(db.snapshots.update().where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.source_id==row['source_id'],db.snapshots.c.topic=='shipments').values(payload=payload))
                return row
        except Exception as error:return self.finish(identifier,who,'uncertain' if sending else 'failed','DIRECT_RESULT_UNCERTAIN' if sending else 'DIRECT_SESSION_REJECTED' if isinstance(error,SessionRejected) else 'DIRECT_RECHECK_FAILED')

class SessionRejected(Exception):pass

def install(app,engine,controller):
    service=DirectPurchases(engine)
    @app.post('/v1/direct/purchases/prepare')
    def prepare(data:Prepare,who=Depends(controller)):return service.prepare(data,who)
    @app.post('/v1/direct/purchases/{identifier}/confirm')
    def confirm(identifier:str,data:Confirm,who=Depends(controller)):return service.confirm(identifier,data,who)
