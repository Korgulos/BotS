import json
from sqlalchemy import select
from swgoh_cloud.direct_wire import field
from swgoh_cloud import db,inventory
from test_direct_purchases import ready
from test_api import setup

def data():
    mod=field(1,'PRIVATE_MOD_ID')+field(2,'252')
    unit=field(1,'PRIVATE_UNIT_ID')+field(2,'LEIA:SEVEN_STAR')+field(21,mod)
    inv=field(9,field(1,1)+field(2,9007199254740993))+field(5,field(1,'unitshard_LEIA')+field(2,17))+field(10,field(1,'001')+field(2,12))+field(13,mod)+field(1,unit)
    return field(2,field(19,392189647)+field(7,'PRIVATE_NAME'))+field(5,inv)+field(51,'PRIVATE_AUTH')

def test_inventory_quantities_and_no_instance_or_auth_data():
    p=inventory.projection(data());rows={(r['type'],r['definition']):r for r in p['items']}
    assert rows['CURRENCY','GRIND']['quantity']=='9007199254740993'
    assert rows['MATERIAL','unitshard_LEIA']['quantity']=='17'
    assert rows['EQUIPMENT','001']['quantity']=='12'
    assert rows['STATMOD','252']['quantity']=='2'
    assert rows['UNIT','LEIA']['quantity']=='1'
    assert 'PRIVATE' not in json.dumps(p)

def test_inventory_missing_section_is_not_empty_inventory():
    import pytest
    with pytest.raises(ValueError):inventory.projection(b'')
    assert inventory.projection(field(5,b''))['items']==[]

def test_inventory_refresh_roles_scope_and_rate_limit(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    monkeypatch.setattr(inventory,'receive',lambda *args:(1,data()))
    assert c.get('/v1/inventory',headers=r).json() is None
    for who in [r,w]:assert c.post('/v1/inventory/refresh',headers=who,json={}).status_code==403
    response=c.post('/v1/inventory/refresh',headers=o,json={});assert response.status_code==200,response.text
    assert response.json()['available'] and 'PRIVATE' not in response.text
    snap=c.get('/v1/inventory',headers=r).json();assert snap['payload']['items'] and snap['topic']=='inventory'
    assert any(x['topic']=='inventory' for x in c.get('/v1/snapshots',headers=r).json())
    assert c.post('/v1/inventory/refresh',headers=o,json={}).status_code==429
    from test_api import ADMIN
    a=c.post('/v1/accounts',headers={'Authorization':'Bearer '+ADMIN},json={'alias':'inventory-other'}).json()
    assert c.get('/v1/inventory',headers={'Authorization':'Bearer '+a['read_token']}).json() is None

def test_inventory_failed_refresh_keeps_previous_snapshot(engine,monkeypatch):
    c,o,w,r,s,b,fresh=ready(engine,monkeypatch)
    inventory.save(engine,dict(account_id=s['_accountId'],source_id=s['_sourceId'],session='a'*64),inventory.projection(data()))
    monkeypatch.setattr(inventory,'receive',lambda *args:(7,b''))
    assert c.post('/v1/inventory/refresh',headers=o,json={}).status_code==409
    assert c.get('/v1/inventory',headers=r).json()['payload']['items']
