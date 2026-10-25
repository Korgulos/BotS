from datetime import datetime,timezone
import httpx,pytest
from swgoh_cloud.local_session import private_read,private_write,run
from swgoh_cloud.direct_wire import fields,field

def session():return dict(capturedAt=datetime.now(timezone.utc).isoformat(),authId='PRIVATE_ID',authToken='PRIVATE_TOKEN',requestToken='PRIVATE_NEXT',hasNextRequestToken=True,clientTime=int(datetime.now(timezone.utc).timestamp()*1000),localState='ready',usedOperations=[])

def test_local_session_private_file_and_live_game_block(tmp_path):
    p=tmp_path/'session.json';private_write(p,session());assert p.stat().st_mode&0o777==0o600
    with pytest.raises(ValueError,match='Close SWGoH'):run('Ping',p,running=lambda:True)
    p.chmod(0o644)
    with pytest.raises(ValueError,match='0600'):private_read(p)
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises(OSError):private_read(link)

def test_ping_once_updates_token_without_showing_it(tmp_path):
    p=tmp_path/'session.json';private_write(p,session());calls=[]
    def handler(r):
        calls.append(r);assert str(r.url)=='https://sw-live.cgswprod.com/rpc'
        f=fields(r.content);assert f[5]==[b'Ping'] and f[7]==[b'PRIVATE_ID'] and f[8]==[b'PRIVATE_TOKEN'] and f[36]==[b'PRIVATE_NEXT']
        return httpx.Response(200,content=field(5,1)+field(13,'ROTATED_PRIVATE')+field(4,field(1,1792950000)))
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result=run('Ping',p,c,running=lambda:False)
        assert result['success'] and result['system_time']==1792950000 and 'PRIVATE' not in str(result)
        assert private_read(p)['requestToken']=='ROTATED_PRIVATE'
        with pytest.raises(ValueError,match='cannot be repeated'):run('Ping',p,c,running=lambda:False)
    assert len(calls)==1

def test_lost_response_never_replayed(tmp_path):
    p=tmp_path/'session.json';private_write(p,session());calls=[]
    def handler(r):calls.append(1);raise httpx.ReadTimeout('contains-secret',request=r)
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        result=run('Ping',p,c,running=lambda:False);assert result['outcome']=='uncertain' and 'secret' not in str(result)
        with pytest.raises(ValueError):run('Ping',p,c,running=lambda:False)
    assert len(calls)==1

def test_unverified_next_token_blocks_sending(tmp_path):
    p=tmp_path/'session.json';private_write(p,{**session(),'hasNextRequestToken':False})
    with pytest.raises(ValueError,match='Next-request token'):run('Ping',p,running=lambda:False)

def test_initial_projection_excludes_identity_tokens_and_personal_extras():
    from swgoh_cloud.local_session import initial_projection
    inventory=field(9,field(1,2)+field(2,220))+field(1,b'UNIT_PRIVATE')
    player=field(3,85)+field(19,392189647)+field(7,'PRIVATE_NAME')
    data=field(2,player)+field(5,inventory)+field(16,field(1,field(2,'REWARD_SUBJECT')+field(8,3)))+field(51,'IDENTITY_SECRET')
    result=initial_projection(data)
    assert result['player']['allyCode']==392189647 and result['currencies'][0]['quantity']==220
    assert result['inventory']['units']==1 and result['inbox'][0]['subjectKey']=='REWARD_SUBJECT'
    assert 'PRIVATE' not in str(result) and 'SECRET' not in str(result)
