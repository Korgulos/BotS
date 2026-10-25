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

@pytest.mark.parametrize('currency',sorted(__import__('swgoh_cloud.currency_policy',fromlist=['ALLOWED']).ALLOWED))
def test_each_allowed_currency_uses_its_own_balance(currency):
    from swgoh_cloud.currency_policy import CURRENCY_IDS
    # A balance in Credits must never cover another currency's cost.
    data=fields(initial(currency=currency))
    data[5]=[field(9,field(1,CURRENCY_IDS[currency])+field(2,1000))]
    payload=b''.join(field(tag,value) for tag,values in data.items() for value in values)
    quote=offers(payload,1000)[0]
    assert quote['currency']==currency and quote['balance']==1000
    if currency!='GRIND':assert offers(initial(currency=currency),1000)==[]

@pytest.mark.parametrize('currency',['PREMIUM','REAL_MONEY','UNKNOWN'])
def test_forbidden_currencies_never_produce_offers_or_packets(currency):
    assert offers(initial(currency=currency),1000)==[]
    quote=offers(initial(refresh=9999999999),1000)[0]
    with pytest.raises(ValueError):purchase_packet({},dict(quote,currency=currency))

def test_mod_requires_verified_five_star_definition_and_fresh_speed_secondary():
    definition='252:STATMOD_TIER_03'
    def payload(speed=50000,primary_only=False,kind=8):
        mod=field(2,'252')+field(12 if primary_only else 13,field(2,field(3,5)+field(5,speed)))
        reward=field(1,definition)+field(2,15)+field(4,1)+field(5,1)+field(7,mod)
        cost=field(1,'GRIND')+field(2,3)+field(4,100)+field(5,100)
        board=field(1,kind)+field(3,2000)+field(2,field(12,'mod1')+field(13,reward)+field(14,cost))
        return field(18,field(10,board))+field(5,field(9,field(1,1)+field(2,1000)))
    metadata={definition:{'verified':True,'stars':5}}
    assert len(offers(payload(),1000,metadata))==1
    assert offers(payload(),1000)==[]
    assert offers(payload(),1000,{definition:{'verified':True,'stars':4}})==[]
    assert offers(payload(speed=0),1000,metadata)==[]
    assert offers(payload(primary_only=True),1000,metadata)==[]
    assert offers(payload(),1000,{'different-mod':metadata[definition]})==[]
