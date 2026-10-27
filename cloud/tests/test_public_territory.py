import json
from test_api import setup,observation
from test_territory import payload
from swgoh_cloud.public_territory import projection

def test_public_table_has_no_account_selector_or_action_access(engine,monkeypatch):
    client,admin,a,s=setup(engine)
    assert client.get('/tb').status_code==404
    monkeypatch.setenv('SWGOH_PUBLIC_TB_ACCOUNT',a['account_id'])
    event=observation(event_key='public:tb',service='Inspector',operation='Snapshot',topic='territory',payload=payload())
    assert client.post('/v1/ingest',headers={'Authorization':'Bearer '+s['ingest_token']},json={'events':[event]}).status_code==200
    other=client.post('/v1/accounts',headers=admin,json={'alias':'other-public-test'}).json()
    response=client.get('/public/tb/phases?account_id='+other['account_id']);assert response.status_code==200
    assert len(response.json()[0]['payload']['rows'])==47
    text=response.text
    for private in [a['account_id'],s['source_id'],a['read_token'],s['ingest_token']]:assert private not in text
    assert set(response.json()[0])=={'payload'}
    page=client.get('/tb');assert page.status_code==200 and 'public-tb.js' in page.text
    assert 'noindex' in page.headers['x-robots-tag']
    for endpoint in ['/public/tb/phases','/tb']:assert client.post(endpoint,json={}).status_code==405
    for endpoint in ['/v1/territory/collect','/v1/inbox/prepare','/v1/maintenance/run']:
        assert client.post(endpoint,json={}).status_code==401
    assert client.get('/v1/inventory').status_code==401

def test_projection_removes_non_table_private_fields():
    p={'battleKey':'a'*32,'phase':1,'state':'live','columns':['power_round_1'], 'authToken':'private-secret', 'source_id':'private-source','rows':[{'memberKey':'private-internal-key','name':'<script>x</script>','playerId':'private-player-id','galacticPower':'5000','values':{'power_round_1':'123','password':'private-password'},'missions':[]}]}
    result=projection(p);text=json.dumps(result)
    for value in ['private-secret','private-source','private-internal-key','private-player-id','private-password']:assert value not in text
    assert result['payload']['rows'][0]['memberKey']=='row-1'
    assert result['payload']['rows'][0]['values']=={'power_round_1':'123'}
