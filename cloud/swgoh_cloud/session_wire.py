"""Verified read-only account request codec; no credential storage or cloud routes."""
import uuid,time,zlib
from datetime import datetime,timezone
from .direct_wire import field,fields as wire_fields
from .direct_probe import RPC_URL
LIMIT=32*1024*1024
INITIAL_SCHEMA_VERIFIED=True
def fields(data):return wire_fields(data,max_bytes=LIMIT)
def build_request(s,operation):
    if operation not in {'Ping','GetInitialData'}:raise ValueError('Read-only operation not allowed')
    if operation=='GetInitialData' and not INITIAL_SCHEMA_VERIFIED:raise ValueError('InitialDataRequest schema must be verified first')
    payload=b'' if operation=='Ping' else field(1,0)
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(s['capturedAt'])).total_seconds()
    client_time=s.get('clientTime',0)
    if client_time<=0:raise ValueError('Client time baseline is missing')
    current=client_time+int(age*(1000 if client_time>100000000000 else 1))
    parts=[field(1,1),field(4,'PlayerRpc'),field(5,operation),field(6,payload),field(7,s['authId']),field(8,s['authToken']),field(16,str(uuid.uuid4())),field(20,current)]
    for name,tag in [('clientVersion',9),('clientStartupTimestamp',11),('platform',12),('region',13),('clientExternalVersion',14),('clientInternalVersion',15),('requestToken',36),('application',37),('contentNamespace',50),('timezone',22),('timezoneOffsetSec',41)]:
        value=s.get(name)
        if value and (not isinstance(value,int) or value>=0):parts.append(field(tag,value))
    return b''.join(parts)

def initial_projection(payload):
    root=fields(payload)
    def child(node,tag):
        values=node.get(tag,[])
        if len(values)>1:raise ValueError('Duplicate initial-data section')
        return fields(values[0]) if values else {}
    def value(node,tag,default=None):
        values=node.get(tag,[])
        if len(values)>1:raise ValueError('Duplicate scalar')
        return values[0] if values else default
    player=child(root,2);inventory=child(root,5);inbox=child(root,16);store=child(root,18)
    currencies=[]
    for blob in inventory.get(9,[]):
        row=fields(blob);currencies.append(dict(currency=value(row,1,1),quantity=value(row,2,0),bonus=value(row,3,0)))
    messages=[]
    for blob in inbox.get(1,[]):
        row=fields(blob);subject=value(row,2,b'').decode('utf8')
        messages.append(dict(subjectKey=subject,type=value(row,8,0),attachments=len(row.get(7,[]))))
    return dict(player=dict(level=value(player,3),allyCode=value(player,19)),currencies=currencies,
        inventory=dict(units=len(inventory.get(1,[])),materials=len(inventory.get(5,[])),equipment=len(inventory.get(10,[])),unequippedMods=len(inventory.get(13,[]))),
        inbox=messages,shipments=len(store.get(10,[])),guildAvailable=40 in root)

def exchange(session,operation,client):
    if session.get('requestTokenMode')!='absent' or session.get('requestToken'):raise ValueError('Sequenced sessions are not supported in cloud tests')
    with client.stream('POST',RPC_URL,content=build_request(session,operation),headers={'Content-Type':'application/x-protobuf','Accept':'application/x-protobuf'}) as r:
        if r.status_code!=200:raise ValueError('HTTP rejection')
        data=bytearray()
        for chunk in r.iter_bytes():
            data.extend(chunk)
            if len(data)>LIMIT:raise ValueError('Response too large')
    envelope=fields(bytes(data))
    def one(tag,default):
        a=envelope.get(tag,[])
        if len(a)>1:raise ValueError('Duplicate envelope field')
        return a[0] if a else default
    code=one(5,0);encoding=one(7,0);payload=one(4,b'')
    if type(code) is not int or type(encoding) is not int or not isinstance(payload,bytes):raise ValueError('Invalid envelope')
    if one(13,b''):raise ValueError('Unexpected token sequencing; local verification required')
    if encoding==1:
        d=zlib.decompressobj(31);payload=d.decompress(payload,LIMIT+1)
        if len(payload)>LIMIT or d.unconsumed_tail or not d.eof:raise ValueError('Compressed payload limit')
    elif encoding!=0:raise ValueError('Unknown encoding')
    result={'operation':operation,'rpc_code':code,'success':code==1,'payload_bytes':len(payload)}
    if code!=1:return result
    if operation=='Ping':result['system_time']=fields(payload).get(1,[None])[0]
    else:
        projection=initial_projection(payload)
        if projection['player']['allyCode']!=session.get('expectedAllyCode'):raise ValueError('Account mismatch')
        result['data']=projection
    return result
