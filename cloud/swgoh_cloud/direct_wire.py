"""Small bounded codec for credential-free ContentRpc.GetMetadata diagnostics.
Field tags are exported from the installed game's protobuf RuntimeTypeModel.
This is not a generic authenticated RPC executor.
"""
import re,zlib
LIMIT=1024*1024

def varint(n):
    if type(n) is not int or not 0<=n<2**64:raise ValueError('Invalid varint')
    b=bytearray()
    while n>=128:b.append((n&127)|128);n>>=7
    b.append(n);return bytes(b)

def field(tag,value):
    if isinstance(value,int):return varint(tag<<3)+varint(value)
    value=value.encode() if isinstance(value,str) else value
    return varint((tag<<3)|2)+varint(len(value))+value

def fields(data,max_bytes=LIMIT):
    if len(data)>max_bytes:raise ValueError('Message too large')
    pos=0;result={};count=0
    def read():
        nonlocal pos
        value=0
        for i in range(10):
            if pos>=len(data):raise ValueError('Truncated varint')
            b=data[pos];pos+=1
            if i==9 and b>1:raise ValueError('Varint overflow')
            value|=(b&127)<<(7*i)
            if b<128:return value
        raise ValueError('Invalid varint')
    while pos<len(data):
        count+=1
        if count>20000:raise ValueError('Too many fields')
        key=read();tag=key>>3;wire=key&7
        if not 0<tag<2**29:raise ValueError('Invalid field tag')
        if wire==0:value=read()
        else:
            if wire==2:n=read()
            elif wire==1:n=8
            elif wire==5:n=4
            else:raise ValueError('Unsupported wire type')
            if n>len(data)-pos:raise ValueError('Truncated field')
            value=data[pos:pos+n];pos+=n
        result.setdefault(tag,[]).append(value)
    return result

def request():
    # Explicit DEFAULT request type = 1. No auth, identity, device or session fields.
    return field(1,1)+field(4,'ContentRpc')+field(5,'GetMetadata')+field(6,field(2,1))

def response(data):
    envelope=fields(data)
    def single(tag,default=None):
        items=envelope.get(tag,[])
        if len(items)>1:raise ValueError('Duplicate envelope field')
        return items[0] if items else default
    code=single(5,0);encoding=single(7,0);payload=single(4,b'')
    if not isinstance(code,int) or not isinstance(encoding,int) or not isinstance(payload,bytes):raise ValueError('Invalid response envelope')
    if encoding==1:
        d=zlib.decompressobj(31);payload=d.decompress(payload,LIMIT+1)
        if len(payload)>LIMIT or d.unconsumed_tail or not d.eof:raise ValueError('Invalid compressed payload')
    elif encoding!=0:raise ValueError('Unknown content encoding')
    result={'rpc_code':code,'rpc_ok':code==1,'payload_bytes':len(payload)}
    if code==1:
        metadata=fields(payload);version={}
        for tag,name in [(9,'localization_version'),(10,'gamedata_version'),(19,'server_version')]:
            items=metadata.get(tag,[])
            if len(items)>1:raise ValueError('Duplicate metadata version')
            if items:
                text=items[0].decode('utf8')
                if not re.fullmatch(r'[A-Za-z0-9_:./+\-]{1,160}',text):raise ValueError('Invalid version')
                version[name]=text
        result['metadata']=version
    return result
