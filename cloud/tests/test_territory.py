from copy import deepcopy
from datetime import datetime, timezone, timedelta
from swgoh_cloud.territory import phase_view
from test_api import setup, observation

M='1'*32; RUN='2'*32

def payload(round=1):
    t=datetime.now(timezone.utc)-timedelta(seconds=20-round)
    def stat(code,score='10',covert=None):
        return {'statId':code,'players':{'count':1,'items':[{'memberKey':M,'member':'Mioni','score':score,'covert':{'count':len(covert or []),'items':covert or [],'truncated':False}}],'truncated':False}}
    return {'available':True,'observedAt':t.isoformat(),'rosterObservedAt':t.isoformat(),
      'members':[{'memberKey':M,'name':'Mioni','galacticPower':'13371682'}]+[{'memberKey':f'{x:032x}','name':'Član '+str(x),'galacticPower':'1000'} for x in range(3,49)],
      'runs':[{'battleKey':RUN,'definition':'t05D','currentRound':round,'statsRound':round,'statusObservedAt':t.isoformat(),'statsObservedAt':t.isoformat(),
       'stats':{'count':5,'truncated':False,'items':[stat('summary_round_1','13967131'),stat('power_round_1','13367131'),stat('strike_attempt_round_1','3'),stat('summary_round_2','0'),stat('covert_results_round_1','0',[{'zone':'zone1','result':'UNATTEMPTED'},{'zone':'zone2','result':'ATTEMPTEDFAILED'},{'zone':'zone3','result':'ATTEMPTEDSUCCEEDED'}])]}}]}

def test_join_all_members_flags_and_no_invented_zeros():
    p=payload();v=phase_view(p,p['runs'][0],1)
    assert len(v['rows'])==47
    m=v['rows'][0];assert m['territoryPoints']=='13967131' and m['undeployedGP']=='4551'
    assert m['played'] is True and m['deployed'] is True and m['donated'] is None
    assert [(x['attempted'],x['completed']) for x in m['missions']]==[(False,False),(True,False),(True,True)]
    missing=v['rows'][1];assert missing['territoryPoints'] is None and missing['played'] is None
    assert 'summary_round_2' not in v['columns']

def test_phase_history_finalization_isolation_and_late_observations(engine):
    c,admin,a,s=setup(engine);w={'Authorization':'Bearer '+s['ingest_token']};r={'Authorization':'Bearer '+a['read_token']}
    def send(p,key):
        e=observation(event_key=key,service='Inspector',operation='Snapshot',topic='territory',payload=p)
        response=c.post('/v1/ingest',headers=w,json={'events':[e]});assert response.status_code==200,response.text
    p=payload();send(p,'tb:first')
    rows=c.get('/v1/territory/phases',headers=r).json();assert len(rows)==1 and rows[0]['payload']['state']=='live'
    assert len(rows[0]['payload']['rows'])==47
    # Advancing status alone closes the last observed snapshot without pretending final stats.
    advanced=deepcopy(p);advanced['runs'][0]['currentRound']=2;advanced['runs'][0]['statusObservedAt']=datetime.now(timezone.utc).isoformat()
    send(advanced,'tb:advance')
    rows=c.get('/v1/territory/phases?phase=1',headers=r).json();assert rows[0]['payload']['state']=='closed_last_seen'
    # Fresh round 2 stats explicitly include historical round 1 values: final checkpoint.
    p2=payload(2);p2['members'][0]['galacticPower']='99999999';send(p2,'tb:round2')
    rows=c.get('/v1/territory/phases?phase=1',headers=r).json();assert rows[0]['payload']['state']=='final'
    assert rows[0]['payload']['rows'][0]['galacticPower']=='13371682'
    assert rows[0]['payload']['rows'][0]['undeployedGP']=='4551'
    send(p,'tb:late-old')
    assert c.get('/v1/territory/phases?phase=1',headers=r).json()[0]['payload']['state']=='final'
    assert c.get('/v1/territory/phases',headers=w).status_code==403
    other=c.post('/v1/accounts',headers=admin,json={'alias':'other-tb'}).json()
    assert c.get('/v1/territory/phases',headers={'Authorization':'Bearer '+other['read_token']}).json()==[]
    assert c.get('/v1/territory/phases?phase=7',headers=r).status_code==422
    assert not any(s['topic']=='territory' for s in c.get('/v1/snapshots',headers=r).json())

def test_six_rounds_and_unknown_results():
    p=payload(6);run=p['runs'][0]
    run['stats']['items']=[{'statId':'summary_round_'+str(i),'players':{'items':[{'memberKey':M,'score':str(i)}]}} for i in range(1,7)]
    assert [phase_view(p,run,i)['rows'][0]['territoryPoints'] for i in range(1,7)]==list('123456')
    run['stats']['items'][0]['players']['items'][0]['covert']={'items':[{'zone':'z','result':'NEW_UNKNOWN'}]}
    assert phase_view(p,run,1)['rows'][0]['missions'][0]['completed'] is None

def test_later_full_roster_repairs_anonymous_labels_without_inventing_historic_gp(engine):
    c,admin,a,s=setup(engine);w={'Authorization':'Bearer '+s['ingest_token']};r={'Authorization':'Bearer '+a['read_token']}
    old=payload();old['members'][0].update(name='',galacticPower='0')
    for key,p in [('partial',old),('full',payload(2))]:
        e=observation(event_key='repair:'+key,service='Inspector',operation='Snapshot',topic='territory',payload=p)
        assert c.post('/v1/ingest',headers=w,json={'events':[e]}).status_code==200
    row=c.get('/v1/territory/phases?phase=1',headers=r).json()[0]['payload']['rows'][0]
    assert row['name']=='Mioni' and row['galacticPower'] is None and row['undeployedGP'] is None
