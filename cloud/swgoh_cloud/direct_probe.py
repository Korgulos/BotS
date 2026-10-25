"""Transport diagnostics only. Does not authenticate, replay or send game RPCs."""
from datetime import datetime,timezone
from time import monotonic
import httpx
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
