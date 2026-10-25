import pytest
from swgoh_cloud.direct_wire import field,fields
from swgoh_cloud.shipment_wire import offers,purchase_packet
from datetime import datetime,timezone

def initial(currency='GRIND',price=100,refresh=2000,purchased=0,requirement='',balance=1000):
    cost=field(1,currency)+field(2,3)+field(4,price)+field(5,price)
    reward=field(1,'salvage')+field(2,11)+field(4,2)+field(5,2)
    item=field(12,'item-1')+field(13,reward)+field(14,cost)+field(17,purchased)+field(18,requirement)
    board=field(1,2)+field(2,item)+field(3,refresh)
    inventory=field(9,field(1,1)+field(2,balance))
    return field(18,field(10,board))+field(5,inventory)

def test_only_fresh_affordable_credit_offers():
    assert offers(initial(),1000)[0]['price']==100
    for kwargs in ({'currency':'PREMIUM'},{'refresh':999},{'purchased':1},{'requirement':'level'},{'balance':50}):
        assert offers(initial(**kwargs),1000)==[]

def test_purchase_packet_verified_tags_and_credit_guard():
    quote=offers(initial(refresh=9999999999),1000)[0]
    session=dict(capturedAt=datetime.now(timezone.utc).isoformat(),clientTime=1792960000,authId='test-id',authToken='test-token')
    envelope=fields(purchase_packet(session,quote))
    assert envelope[4]==[b'StoreRpc'] and envelope[5]==[b'BuyShipmentItem']
    assert fields(envelope[6][0])=={1:[2],2:[b'item-1'],3:[0],4:[0]}
    with pytest.raises(ValueError):purchase_packet(session,{**quote,'currency':'PREMIUM'})
    with pytest.raises(ValueError):purchase_packet(session,{**quote,'refresh':1})

def test_purchase_lost_response_never_repeated(tmp_path):
    import httpx,time
    from swgoh_cloud.local_purchase import commit
    from swgoh_cloud.local_session import private_write,private_read
    path=tmp_path/'session.json';journal=tmp_path/'purchase.json'
    session=dict(capturedAt=datetime.now(timezone.utc).isoformat(),clientTime=1792960000,authId='test-id',authToken='test-token',localState='ready',requestTokenMode='absent',expectedAllyCode=123)
    private_write(path,session)
    payload=initial(refresh=9999999999)+field(2,field(19,123))
    quote=offers(payload)[0]
    private_write(journal,dict(id='approval',quote=quote,state='prepared',expires=time.time()+120,sessionCapturedAt=session['capturedAt']))
    calls=[]
    def handler(r):
        calls.append(fields(r.content)[5][0])
        if len(calls)==1:return httpx.Response(200,content=field(5,1)+field(4,payload))
        raise httpx.ReadTimeout('secret',request=r)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result=commit('approval',True,path,journal,c,lambda:False)
        assert result['state']=='uncertain' and result['retry_allowed'] is False
        with pytest.raises(ValueError):commit('approval',True,path,journal,c,lambda:False)
    assert calls==[b'GetInitialData',b'BuyShipmentItem']
    assert private_read(journal)['state']=='uncertain'
