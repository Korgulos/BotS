import json
from uuid import uuid4
from datetime import timedelta
import httpx,pytest
from sqlalchemy import select
from test_direct_account import fixture
from swgoh_cloud import db,direct_purchases
from swgoh_cloud.direct_wire import field


def ready(engine,monkeypatch):
    c,o,w,r,s=fixture(engine,monkeypatch);s.update(_allowPurchases=True,clientTime=1792960000)
    monkeypatch.setenv('SWGOH_DIRECT_TEST_SESSION',json.dumps(s))
    with engine.begin() as conn:
        row=conn.execute(select(db.offers)).mappings().one()
        reward={**row['reward'],'type':'EQUIPMENT','maxQuantity':6};price={**row['price'],'maxQuantity':2700}
        conn.execute(db.offers.update().values(reward=reward,price=price))
    snapshot=c.get('/v1/snapshots',headers=r).json()[0]
    body=dict(request_id=str(uuid4()),event_id=snapshot['event_id'],ordinal=0)
    fresh=dict(shipment_type=2,item_id='OFFER1',definition='GEAR',quantity=6,price=2700,currency='GRIND',balance=5000,refresh=int(row['refresh']))
    monkeypatch.setattr(direct_purchases.DirectPurchases,'read',lambda self,s:[fresh])
    return c,o,w,r,s,body,fresh


def test_direct_purchase_roles_reservation_confirm_and_idempotency(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    for h in (r,w):assert c.post('/v1/direct/purchases/prepare',headers=h,json=b).status_code==403
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()
    assert cmd['state']=='prepared' and cmd['quote']['balanceAfter']==2300
    assert c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()['id']==cmd['id']
    assert c.post('/v1/direct/purchases/prepare',headers=o,json={**b,'request_id':str(uuid4())}).status_code==409
    assert c.get('/v1/bridge/purchases',headers=w).json() is None
    # Existing native route must not route the direct command through the bridge.
    confirm=dict(confirmed=True,quote_hash=cmd['quote_hash'])
    assert c.post('/v1/purchases/'+cmd['id']+'/confirm',headers=o,json=confirm).status_code==409
    calls=[]
    def receive(client,packet):
        calls.append(packet)
        return 1,field(1,field(2,field(1,field(1,1)+field(2,2700))))
    monkeypatch.setattr(direct_purchases,'receive',receive)
    url='/v1/direct/purchases/'+cmd['id']+'/confirm'
    assert c.post(url,headers=r,json=confirm).status_code==403
    assert c.post(url,headers=o,json=confirm).json()['state']=='succeeded'
    assert c.post(url,headers=o,json=confirm).json()['state']=='succeeded'
    assert len(calls)==1
    assert c.get('/v1/shipments',headers=r).json()[0]['purchased'] is True
    assert c.get('/v1/snapshots',headers=r).json()[0]['payload']['offers']['shipments'][0]['items'][0]['purchased'] is True
    with engine.connect() as conn:assert 'SECRET' not in str([dict(x) for x in conn.execute(select(db.purchases)).mappings()])


def test_direct_uncertain_blocks_replay_and_new_commands(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json();calls=[]
    def receive(*args):calls.append(1);raise httpx.ReadTimeout('SECRET_TOKEN')
    monkeypatch.setattr(direct_purchases,'receive',receive)
    url='/v1/direct/purchases/'+cmd['id']+'/confirm';confirm=dict(confirmed=True,quote_hash=cmd['quote_hash'])
    result=c.post(url,headers=o,json=confirm)
    assert result.json()['state']=='uncertain' and 'SECRET' not in result.text
    assert c.post(url,headers=o,json=confirm).json()['state']=='uncertain'
    assert c.post('/v1/direct/purchases/prepare',headers=o,json={**b,'request_id':str(uuid4())}).status_code==409
    assert len(calls)==1


def test_direct_rechecks_changed_price_without_send(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()
    fresh['price']=2800
    monkeypatch.setattr(direct_purchases,'receive',lambda *args:pytest.fail('Purchase must not be sent'))
    result=c.post('/v1/direct/purchases/'+cmd['id']+'/confirm',headers=o,json=dict(confirmed=True,quote_hash=cmd['quote_hash']))
    assert result.json()['state']=='failed' and result.json()['code']=='DIRECT_RECHECK_FAILED'


def test_direct_offline_scope_expiry_crystals_and_status(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    status=c.get('/v1/purchases',headers=r).json()['direct'];assert status['available'] and 'SECRET' not in str(status)
    with engine.begin() as conn:conn.execute(db.agents.update().values(online=True,last_seen=db.now()))
    assert c.post('/v1/direct/purchases/prepare',headers=o,json=b).status_code==409
    assert not c.get('/v1/purchases',headers=o).json()['direct']['available']
    with engine.begin() as conn:
        conn.execute(db.agents.update().values(online=False))
        conn.execute(db.offers.update().values(price=dict(type='CURRENCY',definition='PREMIUM',minQuantity=2700,maxQuantity=2700)))
    assert c.post('/v1/direct/purchases/prepare',headers=o,json=b).status_code==409
    s['_expiresAt']=(db.now()-timedelta(seconds=1)).isoformat();monkeypatch.setenv('SWGOH_DIRECT_TEST_SESSION',json.dumps(s))
    assert not c.get('/v1/purchases',headers=r).json()['direct']['available']


def test_direct_restart_after_send_keeps_reservation(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()
    with engine.begin() as conn:conn.execute(db.purchases.update().values(state='sending',confirmed_at=db.now(),updated_at=db.now()-timedelta(seconds=70)))
    history=c.get('/v1/purchases',headers=o).json();assert history['commands'][0]['state']=='uncertain'
    monkeypatch.setattr(direct_purchases,'receive',lambda *args:pytest.fail('Must not resend'))
    assert c.post('/v1/direct/purchases/'+cmd['id']+'/confirm',headers=o,json=dict(confirmed=True,quote_hash=cmd['quote_hash'])).json()['state']=='uncertain'

@pytest.mark.parametrize('currency,wire_id,shop',[('PVP_CURRENCY',10,'SHIPMENTPVP'),('RAID_REWARD_CURRENCY_04',46,'SHIPMENTGUILD'),('DATACRON_CURRENCY',40,'SHIPMENTDATACRON'),('ERA_UPGRADE_CURRENCY',50,'SHIPMENTERA')])
def test_direct_other_currencies_and_stores_check_real_debit(engine,monkeypatch,currency,wire_id,shop):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    with engine.begin() as conn:
        conn.execute(db.offers.update().values(shop=shop,price=dict(type='CURRENCY',definition=currency,minQuantity=2700,maxQuantity=2700)))
    fresh.update(currency=currency,shipment_type=next(k for k,v in direct_purchases.SHOPS.items() if v==shop))
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()
    assert cmd['state']=='prepared',cmd
    monkeypatch.setattr(direct_purchases,'receive',lambda *args:(1,field(1,field(2,field(1,field(1,wire_id)+field(2,2700))))))
    result=c.post('/v1/direct/purchases/'+cmd['id']+'/confirm',headers=o,json=dict(confirmed=True,quote_hash=cmd['quote_hash'])).json()
    assert result['state']=='succeeded' and result['quote']['currency']==currency


def test_wrong_debit_currency_never_reported_as_success(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    cmd=c.post('/v1/direct/purchases/prepare',headers=o,json=b).json()
    monkeypatch.setattr(direct_purchases,'receive',lambda *args:(1,field(1,field(2,field(1,field(1,2)+field(2,2700))))))
    result=c.post('/v1/direct/purchases/'+cmd['id']+'/confirm',headers=o,json=dict(confirmed=True,quote_hash=cmd['quote_hash'])).json()
    assert result['state']=='uncertain'
