from pathlib import Path
import json
from datetime import datetime, timezone
import httpx
import pytest
from fastapi.testclient import TestClient
from swgoh_cloud.bridge import Outbox, collect, inspector_address, validate_cloud_url
from swgoh_cloud.api import create_app
from swgoh_cloud.config import Settings


def test_outbox_atomic_enrichment_and_recovery(tmp_path):
    path=tmp_path/'queue.sqlite3';outbox=Outbox(path)
    assert outbox.record('session-001-test','rpc:1','PlayerRpc','Ping',{},None)
    assert not outbox.record('session-001-test','rpc:1','PlayerRpc','Ping',{},None)
    assert outbox.record('session-001-test','rpc:1','PlayerRpc','Ping',{}, {'SystemTime':'9223372036854775807','claimReference':'LOCAL_CAPABILITY'})
    rows=outbox.conn.execute('SELECT body FROM outbox').fetchall();assert len(rows)==2
    enriched=json.loads(rows[1][0]);assert enriched['revision']==2 and 'claimReference' not in rows[1][0]
    assert json.loads(rows[0][0])['observed_at']==enriched['observed_at']
    outbox.close();outbox=Outbox(path)
    def incomplete(request):
        return httpx.Response(200,json={'acknowledged':[]})
    with httpx.Client(transport=httpx.MockTransport(incomplete)) as client:
        with pytest.raises(RuntimeError):outbox.flush(client,'https://test.invalid','S'*48)
    assert outbox.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==2
    def ack(request):
        data=json.loads(request.content)
        return httpx.Response(200,json={'acknowledged':[dict(event_key=e['event_key'],revision=e['revision'],state='inserted') for e in data['events']]})
    with httpx.Client(transport=httpx.MockTransport(ack)) as client:
        assert outbox.flush(client,'https://test.invalid','S'*48)==2
    assert outbox.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==0
    assert not outbox.record('session-001-test','rpc:1','PlayerRpc','Ping',{}, {'SystemTime':'9223372036854775807','claimReference':'LOCAL_CAPABILITY'})
    outbox.close()


def test_bridge_to_real_postgres(engine,tmp_path):
    cloud=TestClient(create_app(Settings('unused','A'*48),engine))
    admin={'Authorization':'Bearer '+'A'*48}
    account=cloud.post('/v1/accounts',headers=admin,json={'alias':'bridge'}).json()
    source=cloud.post(f'/v1/accounts/{account["account_id"]}/sources',headers=admin,json={'name':'home'}).json()
    reads=[]
    def local(request):
        reads.append((request.method,request.url.path))
        assert request.method=='GET'
        if request.url.path=='/api/http':return httpx.Response(200,json={'requests':[
            {'id':1,'state':'Finished','service':'AuthRpc','operation':'DoAuthNucleus','headers':{'secret':'NO_UPLOAD'}},
            {'id':2,'state':'Finished','service':'BattleRpc','operation':'BattleFinish','detailsAvailable':True,'status':200,'firstSeen':datetime.now(timezone.utc).isoformat()}]})
        if request.url.path=='/api/http-detail':return httpx.Response(200,json={'ready':True,'details':{'kind':'battle','phase':'finish','request':{'type':'TERRITORYWARBATTLE','outcome':'WIN','recordCount':435,'records':[],'recordsTruncated':True,'state':{'data':{'battleSeed':-900919326}}}}})
        if request.url.path=='/api/shipments':return httpx.Response(200,json={'available':True,'offers':{'shipments':[]}})
        if request.url.path=='/api/inbox':return httpx.Response(200,json={'captured':True,'inbox':{'messages':[{'number':1,'type':'RANKREWARD','claimReference':'NO_UPLOAD','attachments':[]}]}})
        return httpx.Response(200,json={'rewards':[],'busy':False,'readAt':datetime.now(timezone.utc).isoformat()})
    queue=Outbox(tmp_path/'queue.sqlite3')
    with httpx.Client(transport=httpx.MockTransport(local)) as client:
        collect(client,'http://127.0.0.1:18181','K'*48,'inspector-session-test',queue)
    assert queue.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==4
    attempts=[0]
    def upload(request):
        assert b'NO_UPLOAD' not in request.content
        response=cloud.post('/v1/ingest',headers={'Authorization':request.headers['Authorization']},json=json.loads(request.content))
        attempts[0]+=1
        if attempts[0]==1:
            raise httpx.ReadTimeout("Simulated lost acknowledgement",request=request)
        return httpx.Response(response.status_code,json=response.json())
    with httpx.Client(transport=httpx.MockTransport(upload)) as client:
        with pytest.raises(httpx.ReadTimeout):queue.flush(client,'https://cloud.invalid',source['ingest_token'])
        assert queue.conn.execute('SELECT count(*) FROM outbox').fetchone()[0]==4
        assert queue.flush(client,'https://cloud.invalid',source['ingest_token'])==4
    reader={'Authorization':'Bearer '+account['read_token']}
    assert len(cloud.get('/v1/snapshots',headers=reader).json())==3
    assert cloud.get('/v1/battles',headers=reader).json()[0]['outcome']=='WIN'
    assert not any(method!='GET' for method,path in reads)
    queue.close()


def test_inspector_and_cloud_urls(tmp_path):
    path=tmp_path/'url.txt';path.write_text('http://127.0.0.1:18181/#'+'A'*48)
    base,key,session=inspector_address(path);assert len(session)==64 and key not in session
    for bad in ['https://127.0.0.1:18181/#'+'A'*48,'http://example.com:18181/#'+'A'*48,'http://127.0.0.1:18181/?token=secret#'+'A'*48]:
        path.write_text(bad)
        with pytest.raises(ValueError):inspector_address(path)
    for bad in ['http://example.com','https://user:password@example.com','https://example.com?token=secret']:
        with pytest.raises(ValueError):validate_cloud_url(bad)
    assert validate_cloud_url('https://test.onrender.com/')=='https://test.onrender.com'
