import httpx
from swgoh_cloud.bridge import Outbox

def test_poison_rpc_is_retained_without_blocking_later_snapshot(tmp_path):
    out=Outbox(tmp_path/'out.sqlite')
    out.record('session-test-0001','rpc:bad','BattleRpc','BattleFinish',{}, {'value':1})
    out.record('session-test-0001','snapshot:territory','Inspector','Snapshot',{}, {'available':True},'territory')
    def reply(req):
        import json
        rows=json.loads(req.content)['events']
        if len(rows)>1 or rows[0]['event_key']=='rpc:bad':return httpx.Response(409,json={'detail':'Conflicting event revision'})
        return httpx.Response(200,json={'acknowledged':[{'event_key':rows[0]['event_key'],'revision':rows[0]['revision'],'state':'inserted'}]})
    with httpx.Client(transport=httpx.MockTransport(reply)) as c:assert out.flush(c,'https://example.test','fake')==2
    assert out.conn.execute('select count(*) from outbox').fetchone()[0]==0
    body,reason=out.conn.execute('select body,reason from conflicting_observations').fetchone()
    assert 'BattleFinish' in body and reason=='cloud_revision_conflict'
    out.close()
