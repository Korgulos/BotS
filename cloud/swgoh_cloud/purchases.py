"""A source-bound purchase queue. No game credentials or native approval tokens here."""
from datetime import timedelta, datetime, timezone
import hashlib
import json
from typing import Literal
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import db
from .models import StrictModel
from .mod_policy import is_mod, eligible_mod
from .inbox_policy import preview, message_hash

ALLOWED = {'GRIND','PVP_CURRENCY','WAR_SHIP_CURRENCY','FORCE_POINT','PVP_SHIP_CURRENCY','SHARD_CURRENCY','GUILD_CURRENCY','RAID_REWARD_CURRENCY_01','RAID_REWARD_CURRENCY_02','RAID_REWARD_CURRENCY_03','EPISODE_CURRENCY','SHIP_GRIND','TERRITORY_BATTLE_CURRENCY','TERRITORY_BATTLE_CURRENCY_02','TERRITORY_BATTLE_CURRENCY_03','SOCIAL','SEASONS_CURRENCY','CONQUEST_CURRENCY','GL_EVENT_CURRENCY','ERA_CURRENCY'}
TERMINAL = {'succeeded','failed','expired','cancelled'}
ACTIVE = {'queued_prepare','preparing','prepared','queued_commit','sending','uncertain','checking_commit'}

def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

class Prepare(StrictModel):
    request_id: UUID
    event_id: UUID
    ordinal: int = Field(ge=0,le=16000)
class Confirm(StrictModel):
    quote_hash: str = Field(pattern=r'^[a-f0-9]{64}$')
    confirmed: Literal[True]
class Heartbeat(StrictModel):
    session: str = Field(pattern=r'^[a-f0-9]{64}$')
    online: bool
    purchases_enabled: bool
class Claim(StrictModel):
    session: str = Field(pattern=r'^[a-f0-9]{64}$')
    phase: Literal['prepare','commit']
class PublicQuote(StrictModel):
    shop: str = Field(min_length=1,max_length=80)
    itemId: str = Field(min_length=1,max_length=160)
    reward: str = Field(min_length=1,max_length=160)
    quantity: int = Field(gt=0,le=2147483647)
    currency: str = Field(min_length=1,max_length=80)
    price: int = Field(gt=0,le=2147483647)
    balance: int = Field(ge=0,le=2147483647)
    balanceAfter: int = Field(ge=0,le=2147483647)
class InboxReward(StrictModel):
    type: Literal["CURRENCY","MATERIAL","EQUIPMENT"]
    definition: str = Field(min_length=1,max_length=160)
    quantity: int = Field(gt=0)
class InboxQuote(StrictModel):
    kind: Literal["inbox"]
    type: str = Field(max_length=100)
    subjectKey: str = Field(max_length=160)
    starts: str = Field(max_length=32)
    expires: str = Field(max_length=32)
    rewards: list[InboxReward] = Field(min_length=1,max_length=32)
class Report(StrictModel):
    session: str = Field(pattern=r'^[a-f0-9]{64}$')
    state: Literal['prepared','succeeded','failed','uncertain']
    code: str = Field(default='',max_length=160,pattern=r'^[A-Za-z0-9_. -]*$')
    quote: PublicQuote | InboxQuote | None = None
    expires_at: datetime | None = None


def install(app, engine, reader, controller, writer):
    router=APIRouter()
    def own(conn,identifier,who,device=False):
        query=select(db.purchases).where(db.purchases.c.id==identifier,db.purchases.c.account_id==who['account_id'])
        if device:query=query.where(db.purchases.c.source_id==who['source_id'])
        row=conn.execute(query.with_for_update()).mappings().first()
        if row is None:raise HTTPException(404,'Kupovina nije pronađena')
        return dict(row)
    def expire(conn):
        conn.execute(db.purchases.update().where(db.purchases.c.state.in_(['queued_prepare','preparing','prepared','queued_commit']),db.purchases.c.expires_at<db.now()).values(state='expired',code='APPROVAL_EXPIRED',updated_at=db.now()))
        # A claimed send is never requeued, including after a device disappears.
        conn.execute(db.purchases.update().where(db.purchases.c.state.in_(['sending','checking_commit']),db.purchases.c.updated_at<db.now()-timedelta(seconds=60)).values(state='uncertain',code='RESULT_TIMEOUT',updated_at=db.now()))
    def live(conn,source,session=None):
        a=conn.execute(select(db.agents).where(db.agents.c.source_id==source)).mappings().first()
        if not a or not a['online'] or not a['purchases_enabled'] or a['last_seen']<db.now()-timedelta(seconds=20) or session is not None and a['session']!=session:
            raise HTTPException(409,'Igra i bridge moraju biti povezani sa dozvoljenim kupovinama')
        return a
    def change(conn,row,**fields):
        fields['updated_at']=db.now();conn.execute(db.purchases.update().where(db.purchases.c.id==row['id']).values(**fields));row.update(fields);return row

    @router.get('/v1/purchases')
    def history(who=Depends(reader)):
        with engine.begin() as c:
            expire(c)
            rows=[dict(r) for r in c.execute(select(db.purchases).where(db.purchases.c.account_id==who['account_id']).order_by(db.purchases.c.created_at.desc()).limit(30)).mappings()]
            statuses=[dict(r) for r in c.execute(select(db.agents).join(db.sources,db.sources.c.id==db.agents.c.source_id).where(db.sources.c.account_id==who['account_id'])).mappings()]
            from .direct_purchases import status
            direct=status(c,who)
        return {'can_purchase':who['role']=='control','commands':rows,'bridges':statuses,'direct':direct}

    @router.post('/v1/purchases/prepare')
    def prepare(data:Prepare,who=Depends(controller)):
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            expire(c)
            prior=c.execute(select(db.purchases).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.request_id==str(data.request_id))).mappings().first()
            if prior:
                if prior['event_id']!=str(data.event_id) or prior['ordinal']!=data.ordinal:raise HTTPException(409,'ID zahteva je već upotrebljen')
                return dict(prior)
            if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():raise HTTPException(409,'Prethodna kupovina nije završena. Proveri njen ishod; ne ponavljaj kupovinu.')
            row=c.execute(select(db.offers,db.snapshots.c.source_id).join(db.snapshots,db.snapshots.c.event_id==db.offers.c.event_id).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.topic=='shipments',db.offers.c.event_id==str(data.event_id),db.offers.c.ordinal==data.ordinal)).mappings().first()
            if not row:raise HTTPException(409,'Potrebna je trenutna lista ponuda')
            price=row['price'] or {};reward=row['reward'] or {};currency=price.get('definition')
            if currency not in ALLOWED or price.get('type')!='CURRENCY':raise HTTPException(409,'Crystals, novac i nepoznate valute su blokirani')
            if is_mod(row['shop'],reward) and not eligible_mod(reward):raise HTTPException(409,'Dozvoljeni su samo potvrđeni modovi sa 5★ i Speed sekundarnim statom')
            cost=price.get('minQuantity');quantity=reward.get('minQuantity')
            if row['purchased'] is not False or type(cost) is not int or type(quantity) is not int or not 0<cost<=2147483647 or not 0<quantity<=2147483647 or cost!=price.get('maxQuantity',cost) or quantity!=reward.get('maxQuantity',quantity) or not row['definition']:
                raise HTTPException(409,'Ponuda nema potvrđenu cenu i količinu')
            try:refresh=int(row['refresh'])
            except (TypeError,ValueError):raise HTTPException(409,'Osveži listu u igri')
            if refresh<=db.now().timestamp():raise HTTPException(409,'Ponuda je istekla; osveži listu u igri')
            a=live(c,row['source_id'])
            expected=dict(shop=row['shop'],itemId=row['offer_id'],reward=row['definition'],quantity=quantity,currency=currency,price=cost)
            result=dict(id=db.uid(),account_id=who['account_id'],source_id=row['source_id'],session=a['session'],request_id=str(data.request_id),event_id=str(data.event_id),ordinal=data.ordinal,state='queued_prepare',expected=expected,quote=None,quote_hash=None,code=None,created_at=db.now(),updated_at=db.now(),expires_at=db.now()+timedelta(seconds=45),confirmed_at=None)
            c.execute(db.purchases.insert().values(**result));return result

    @router.post('/v1/inbox/prepare')
    def prepare_inbox(data:Prepare,who=Depends(controller)):
        with engine.begin() as c:
            c.execute(select(db.accounts.c.id).where(db.accounts.c.id==who['account_id']).with_for_update()).one()
            expire(c)
            prior=c.execute(select(db.purchases).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.request_id==str(data.request_id))).mappings().first()
            if prior:
                if prior['event_id']!=str(data.event_id) or prior['ordinal']!=data.ordinal or prior['expected'].get('kind')!='inbox':raise HTTPException(409,'ID zahteva je već upotrebljen')
                return dict(prior)
            if c.execute(select(db.purchases.c.id).where(db.purchases.c.account_id==who['account_id'],db.purchases.c.state.in_(ACTIVE))).first():raise HTTPException(409,'Prethodna akcija nije završena; ne ponavljaj zahtev')
            snap=c.execute(select(db.snapshots).where(db.snapshots.c.account_id==who['account_id'],db.snapshots.c.topic=='inbox',db.snapshots.c.event_id==str(data.event_id))).mappings().first()
            if not snap:raise HTTPException(409,'Potrebna je trenutna inbox lista')
            try:
                message=snap['payload']['inbox']['messages'][data.ordinal]
                q=preview(message)
                if not int(q['starts'])<=db.now().timestamp()<int(q['expires']):raise ValueError('Expired')
            except (ValueError,KeyError,IndexError,TypeError):raise HTTPException(409,'Poruka nije podržana, potpuna ili aktivna')
            a=live(c,snap['source_id'])
            expected=dict(kind='inbox',preview=q,message_hash=message_hash(message))
            result=dict(id=db.uid(),account_id=who['account_id'],source_id=snap['source_id'],session=a['session'],request_id=str(data.request_id),event_id=str(data.event_id),ordinal=data.ordinal,state='queued_prepare',expected=expected,quote=None,quote_hash=None,code=None,created_at=db.now(),updated_at=db.now(),expires_at=db.now()+timedelta(seconds=45),confirmed_at=None)
            c.execute(db.purchases.insert().values(**result));return result

    @router.post('/v1/purchases/{identifier}/confirm')
    def confirm(identifier:str,data:Confirm,who=Depends(controller)):
        with engine.begin() as c:
            expire(c);row=own(c,identifier,who)
            if row['expected'].get('transport')=='direct':raise HTTPException(409,'Koristi direktnu potvrdu kupovine')
            if data.quote_hash!=row['quote_hash']:raise HTTPException(409,'Ponuda ili potvrda nisu važeći')
            if row['confirmed_at'] is not None:return row  # Lost ACK never creates another send.
            if row['state']!='prepared' or row['expires_at']<=db.now():raise HTTPException(409,'Potvrda je istekla ili kupovina nije spremna')
            live(c,row['source_id'],row['session'])
            return change(c,row,state='queued_commit',confirmed_at=db.now())

    @router.post('/v1/purchases/{identifier}/cancel')
    def cancel(identifier:str,who=Depends(controller)):
        with engine.begin() as c:
            row=own(c,identifier,who)
            if row['state'] not in {'queued_prepare','preparing','prepared'}:raise HTTPException(409,'Poslata ili nejasna kupovina se ne može otkazati')
            return change(c,row,state='cancelled',code='USER_CANCELLED')

    @router.post('/v1/bridge/heartbeat')
    def heartbeat(data:Heartbeat,who=Depends(writer)):
        values=dict(source_id=who['source_id'],last_seen=db.now(),**data.model_dump())
        with engine.begin() as c:
            c.execute(insert(db.agents).values(**values).on_conflict_do_update(index_elements=['source_id'],set_=values))
        return {'ok':True}

    @router.get('/v1/bridge/purchases')
    def pending(who=Depends(writer)):
        with engine.begin() as c:
            expire(c)
            row=c.execute(select(db.purchases).where(db.purchases.c.source_id==who['source_id'],db.purchases.c.state.in_(ACTIVE),db.purchases.c.expected['transport'].as_string().is_distinct_from('direct')).order_by(db.purchases.c.created_at).limit(1)).mappings().first()
            return dict(row) if row else None

    @router.post('/v1/bridge/purchases/{identifier}/claim')
    def claim(identifier:str,data:Claim,who=Depends(writer)):
        with engine.begin() as c:
            expire(c);row=own(c,identifier,who,True)
            if row['expected'].get('transport')=='direct':raise HTTPException(409,'Direktnu kupovinu ne preuzima bridge')
            if row['session']!=data.session:raise HTTPException(409,'Igra je ponovo pokrenuta; zahtev se ne šalje')
            live(c,who['source_id'],data.session)
            states={'prepare':('queued_prepare','preparing'),'commit':('queued_commit','sending')};before,after=states[data.phase]
            if row['state']==after:return row
            if row['state']!=before or row['expires_at']<=db.now():raise HTTPException(409,'Zahtev nije spreman ili je istekao')
            return change(c,row,state=after)

    @router.post('/v1/bridge/purchases/{identifier}/report')
    def report(identifier:str,data:Report,who=Depends(writer)):
        with engine.begin() as c:
            row=own(c,identifier,who,True)
            if row['session']!=data.session:raise HTTPException(409,'Pogrešna sesija igre')
            if row['state'] in TERMINAL:return row
            if data.state=='prepared':
                if row['state'] not in {'preparing','prepared'} or data.quote is None or data.expires_at is None:raise HTTPException(409,'Neočekivana ponuda')
                q=data.quote.model_dump()
                if row['expected'].get('kind')=='inbox':
                    if q!=row['expected']['preview']:raise HTTPException(409,'Inbox prilozi su promenjeni')
                elif not isinstance(data.quote,PublicQuote) or any(q.get(k)!=v for k,v in row['expected'].items()) or q['currency'] not in ALLOWED or q['balance']-q['price']!=q['balanceAfter']:raise HTTPException(409,'Ponuda se razlikuje od izabranog predmeta')
                if data.expires_at.tzinfo is None:raise HTTPException(422,'Potrebna je vremenska zona')
                expires=min(data.expires_at,db.now()+timedelta(seconds=30),row['expires_at'])
                if expires<=db.now():return change(c,row,state='expired',code='APPROVAL_EXPIRED')
                if row['state']=='prepared':return row
                return change(c,row,state='prepared',quote=q,quote_hash=fingerprint(q),expires_at=expires,code='READY')
            if data.state in {'succeeded','uncertain'} and row['state'] not in {'sending','uncertain'}:raise HTTPException(409,'Kupovina nije potvrđena za slanje')
            return change(c,row,state=data.state,code=data.code or data.state.upper())
    app.include_router(router)
