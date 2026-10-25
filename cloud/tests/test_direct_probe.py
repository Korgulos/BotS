import httpx
from swgoh_cloud.direct_probe import probe,RPC_URL
from test_purchases import prepare_fixture

def test_probe_fixed_target_no_credentials_or_redirect():
    seen=[]
    def handler(request):
        seen.append(request)
        assert str(request.url)==RPC_URL and request.method=='HEAD' and not request.content
        assert not any(k in request.headers for k in ['authorization','cookie','x-inspector-key'])
        return httpx.Response(302,headers={'location':'https://elsewhere.invalid','set-cookie':'private'},content=b'private')
    with httpx.Client(transport=httpx.MockTransport(handler),follow_redirects=False) as client:r=probe(client)
    assert len(seen)==1 and r['transport']=='https_response' and r['http_status']==302
    assert not r['authenticated'] and not r['rpc_sent'] and 'private' not in str(r)

def test_probe_failure_is_not_login_success():
    def handler(request):raise httpx.ConnectTimeout('private-detail',request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:r=probe(client)
    assert r['transport']=='unavailable' and r['error_type']=='ConnectTimeout' and 'private-detail' not in str(r)

def test_probe_requires_control(engine,monkeypatch):
    from swgoh_cloud import direct_probe
    c,a,o,w,r,b,source=prepare_fixture(engine)
    monkeypatch.setattr(direct_probe,'probe',lambda client:{'authenticated':False,'rpc_sent':False,'transport':'test'})
    assert c.post('/v1/direct/probe',headers=r).status_code==403
    assert c.post('/v1/direct/probe',headers=w).status_code==403
    assert c.post('/v1/direct/probe',headers=o).json()['transport']=='test'
