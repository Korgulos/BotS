"""Shipment wire projection verified against runtime schema; no network executor.
First direct trial is restricted to fixed-price Credits offers without requirements.
"""
import time,zlib
from .session_wire import fields,build_request,LIMIT
from .direct_probe import RPC_URL
from .direct_wire import field

def one(node,tag,default=None):
    values=node.get(tag,[])
    if len(values)>1:raise ValueError('Duplicate shipment field')
    return values[0] if values else default

def child(node,tag):return fields(one(node,tag,b''))
def bucket(blob):
    n=fields(blob)
    return {'definition':one(n,1,b'').decode('utf8'),'type':one(n,2,1),'min':one(n,4,0),'max':one(n,5,0)}

def offers(payload,now=None):
    now=int(time.time()) if now is None else now
    root=fields(payload);store=child(root,18);inventory=child(root,5)
    balances={}
    for blob in inventory.get(9,[]):
        n=fields(blob);currency=one(n,1,1)
        if currency in balances:raise ValueError('Duplicate currency')
        balances[currency]=one(n,2,0)
    rows=[];seen=set()
    for blob in store.get(10,[]):
        board=fields(blob);kind=one(board,1,2);refresh=one(board,3,0)
        for raw in board.get(2,[]):
            item=fields(raw);identifier=one(item,12,b'').decode('utf8')
            if (kind,identifier) in seen:raise ValueError('Duplicate offer')
            seen.add((kind,identifier))
            if not identifier or one(item,17,0)!=0 or one(item,18,b''):continue
            if not now<refresh or one(item,15,0)>now or 0<one(item,16,0)<=now:continue
            cost=bucket(one(item,14,b''));reward=bucket(one(item,13,b''))
            if cost['type']!=3 or cost['definition']!='GRIND' or not 0<cost['min']==cost['max']<=2147483647:continue
            if reward['type'] not in (3,7,11) or not reward['definition'] or not 0<reward['min']==reward['max']:continue
            if balances.get(1,0)<cost['max']:continue
            rows.append({'shipment_type':kind,'item_id':identifier,'definition':reward['definition'],'quantity':reward['max'],'price':cost['max'],'currency':'GRIND','balance':balances[1],'refresh':refresh})
    return rows

def purchase_packet(session,quote):
    if quote.get('currency')!='GRIND' or not 0<quote['price']<=quote['balance']:raise ValueError('Only affordable Credits offers allowed')
    if not 1<=quote['shipment_type']<=16 or not quote.get('item_id'):raise ValueError('Invalid offer')
    if quote['refresh']<=time.time():raise ValueError('Offer expired')
    # Reuse the verified authenticated envelope, replacing only service/method/body.
    envelope=fields(build_request(session,'Ping'))
    envelope[4]=[b'StoreRpc'];envelope[5]=[b'BuyShipmentItem']
    envelope[6]=[field(1,quote['shipment_type'])+field(2,quote['item_id'])+field(3,0)+field(4,0)]
    return b''.join(field(tag,value) for tag,values in envelope.items() for value in values)


def receive(client,packet):
    with client.stream('POST',RPC_URL,content=packet,headers={'Content-Type':'application/x-protobuf','Accept':'application/x-protobuf'}) as r:
        if r.status_code!=200:raise ValueError('HTTP rejection')
        data=bytearray()
        for chunk in r.iter_bytes():
            data.extend(chunk)
            if len(data)>LIMIT:raise ValueError('Response too large')
    root=fields(bytes(data));code=one(root,5,0);encoding=one(root,7,0);payload=one(root,4,b'')
    if one(root,13,b''):raise ValueError('Unexpected request sequencing')
    if type(code) is not int or not isinstance(payload,bytes):raise ValueError('Malformed response')
    if encoding==1:
        d=zlib.decompressobj(31);payload=d.decompress(payload,LIMIT+1)
        if len(payload)>LIMIT or d.unconsumed_tail or not d.eof:raise ValueError('Compressed response limit')
    elif encoding!=0:raise ValueError('Unknown encoding')
    return code,payload

