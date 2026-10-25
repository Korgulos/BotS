"""Transport diagnostics only. Does not authenticate, replay or send game RPCs."""
from datetime import datetime,timezone
from time import monotonic
import httpx,zlib
from fastapi import Depends

RPC_URL='https://sw-live.cgswprod.com/rpc'

def probe(client):
    started=monotonic()
    result=dict(endpoint=RPC_URL,checked_at=datetime.now(timezone.utc).isoformat(),authenticated=False,rpc_sent=False)
    try:
        # HEAD has no protobuf body, authentication headers, cookies, or account data.
        response=client.head(RPC_URL)
        return {**result,'transport':'https_response','http_status':response.status_code,'elapsed_ms':round((monotonic()-started)*1000),'redirect_followed':False}
    except httpx.HTTPError as error:
        return {**result,'transport':'unavailable','error_type':type(error).__name__,'elapsed_ms':round((monotonic()-started)*1000)}

def install(app,controller):
    @app.post('/v1/direct/probe')
    def direct_probe(who=Depends(controller)):
        with httpx.Client(timeout=10,follow_redirects=False,trust_env=False) as client:
            return probe(client)

    @app.post('/v1/direct/metadata-probe')
    def direct_metadata(who=Depends(controller)):
        with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
            return metadata_probe(client)


def metadata_probe(client):
    from .direct_wire import request,response,LIMIT
    result=dict(endpoint=RPC_URL,authenticated=False,rpc_sent=True,service='ContentRpc',operation='GetMetadata')
    try:
        with client.stream('POST',RPC_URL,content=request(),headers={'Content-Type':'application/x-protobuf','Accept':'application/x-protobuf'}) as upstream:
            result['http_status']=upstream.status_code
            if upstream.status_code!=200:return {**result,'rpc_ok':False}
            data=bytearray()
            for chunk in upstream.iter_bytes():
                data.extend(chunk)
                if len(data)>LIMIT:raise ValueError('Response too large')
        return {**result,**response(bytes(data))}
    except (httpx.HTTPError,ValueError,UnicodeError,zlib.error) as error:
        return {**result,'rpc_ok':False,'error_type':type(error).__name__}
