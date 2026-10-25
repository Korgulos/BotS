"""Manual local-only existing-session probe. Never imported by the cloud API.
No login, account binding, purchases, claims, or retries. No token values in output.
"""
import argparse,json,os,stat,fcntl,time,uuid,importlib.util
from datetime import datetime,timezone
from pathlib import Path
import httpx
from .bridge import inspector_address
from .direct_wire import field,fields as wire_fields
LIMIT=32*1024*1024
def fields(data):return wire_fields(data,max_bytes=LIMIT)
from .direct_probe import RPC_URL

INITIAL_SCHEMA_VERIFIED=True # Runtime InitialDataRequest export: app_start bool = 1.
STATE=Path(__file__).resolve().parents[1]/'.state'/'direct-session.local.json'

def private_write(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    tmp=path.with_name(path.name+'.'+uuid.uuid4().hex+'.tmp')
    fd=os.open(tmp,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
    try:
        with os.fdopen(fd,'w') as f:json.dump(data,f);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
    finally:
        tmp.unlink(missing_ok=True)

def private_read(path):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info=os.fstat(f.fileno())
        if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or not stat.S_ISREG(info.st_mode):raise ValueError('Session file must be owned by you with mode 0600')
        return json.load(f)

def game_running():
    spec=importlib.util.spec_from_file_location('local_game_watch',Path(__file__).resolve().parents[1]/'watch-game.py')
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
    return bool(m.game_processes())

def capture(config,path=STATE):
    base,key,_=inspector_address(config['inspector_url_file'])
    with httpx.Client(timeout=10,trust_env=False,follow_redirects=False) as c:
        r=c.post(base+'/api/session/export',headers={'X-Inspector-Key':key,'Origin':base},json={'confirmed':True});r.raise_for_status();d=r.json()
    if d.get('error') or not d.get('authId') or not d.get('authToken'):raise ValueError('A recent completed game Ping is required')
    if not d.get('requestToken') and not d.get('hasNextRequestToken'):d['requestTokenMode']='absent'
    d['localState']='ready';d['usedOperations']=[]
    private_write(path,d)
    return {'captured':True,'credential_values_shown':False,'has_next_request_token':d.get('hasNextRequestToken',False)}

from .session_wire import build_request,initial_projection

def run(operation,path=STATE,client=None,running=game_running,allow_read_retry=False,phase="initial"):
    if running():raise ValueError('Close SWGoH before direct session tests')
    if phase not in {'initial','ea-app-closed'} or phase!='initial' and operation not in {'Ping','IsSessionActive'}:raise ValueError('Unsupported read-only check phase')
    path=Path(path)
    lock=os.open(path.with_name(path.name+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        s=private_read(path)
        if not s.get('requestToken') and not s.get('hasNextRequestToken'):s['requestTokenMode']='absent'
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(s['capturedAt'])).total_seconds()
        if age<0 or age>600:raise ValueError('Local test window expired; capture a fresh session')
        read_retry=allow_read_retry and operation=='GetInitialData' and s.get('requestTokenMode')=='absent' and s.get('lastSuccess')=='Ping' and s.get('localState')=='uncertain' and not s.get('initialReadRetry')
        phase_key=phase+':'+operation
        named_check=phase=='ea-app-closed' and s.get('requestTokenMode')=='absent' and s.get('localState')=='ready' and 'Ping' in s.get('successfulOperations',[]) and phase_key not in s.get('phaseAttempts',[])
        if (s.get('localState')!='ready' or operation in s.get('usedOperations',[])) and not (read_retry or named_check):raise ValueError('Previous attempt cannot be repeated; capture a new session')
        if read_retry:s['initialReadRetry']=1
        if operation=='GetInitialData' and s.get('lastSuccess')!='Ping' and 'Ping' not in s.get('successfulOperations',[]):raise ValueError('Successful direct Ping required first')
        if not s.get('hasNextRequestToken') and not (s.get('requestTokenMode')=='absent' and not s.get('requestToken')):raise ValueError('Next-request token not verified; direct test blocked')
        packet=build_request(s,operation)
        s['localState']='sending';s['usedOperations'].append(operation);s.setdefault('phaseAttempts',[]).append(phase_key);private_write(path,s)
        stage='connect';received=False
        own=client is None
        c=client or httpx.Client(timeout=15,follow_redirects=False,trust_env=False)
        try:
            with c.stream('POST',RPC_URL,content=packet,headers={'Content-Type':'application/x-protobuf','Accept':'application/x-protobuf'}) as r:
                if r.status_code!=200:raise ValueError('Upstream did not return HTTP 200')
                stage='read_response';data=bytearray()
                for part in r.iter_bytes():
                    data.extend(part)
                    if len(data)>LIMIT:raise ValueError('Response exceeds limit')
            received=True;stage='decode_envelope'
            if operation=='GetInitialData':private_write(path.with_name('initial-wire.private.json'),{'wire_hex':data.hex()})
            envelope=fields(bytes(data))
            def single(tag,default):
                items=envelope.get(tag,[])
                if len(items)>1:raise ValueError('Duplicate response field')
                return items[0] if items else default
            code=single(5,0);encoding=single(7,0);payload=single(4,b'');next_token=single(13,b'')
            if type(code) is not int or type(encoding) is not int or not isinstance(payload,bytes) or not isinstance(next_token,bytes) or len(next_token)>32768:raise ValueError('Malformed response envelope')
            stage='decompress_payload'
            if encoding==1:
                import zlib
                d=zlib.decompressobj(31);payload=d.decompress(payload,LIMIT+1)
                if len(payload)>LIMIT or d.unconsumed_tail or not d.eof:raise ValueError('Invalid compressed response')
            elif encoding!=0:raise ValueError('Unknown response encoding')
            if code!=1:
                s['localState']='rejected';private_write(path,s);return {'operation':operation,'rpc_code':code,'success':False,'retry_allowed':False}
            stage='decode_payload';decoded=fields(payload)
            s['localState']='ready' if next_token or (s.get('requestTokenMode')=='absent' and not s.get('requestToken')) else 'no_next_token';s['lastSuccess']=operation;s.setdefault('successfulOperations',[]).append(operation)
            if next_token:s['requestToken']=next_token.decode();s['hasNextRequestToken']=True
            private_write(path,s)
            result={'operation':operation,'rpc_code':code,'success':True,'payload_bytes':len(payload),'next_token_received':bool(next_token),'capture_age_seconds':round(age)}
            if operation=='Ping':result['system_time']=decoded.get(1,[None])[0]
            elif operation=='IsSessionActive':
                values=decoded.get(1,[0])
                if len(values)!=1 or type(values[0]) is not int or values[0] not in (0,1):raise ValueError('Invalid active flag')
                result['active']=bool(values[0])
            else:
                # Private payload saved locally; no raw data or account fields in stdout.
                out=path.with_name('initial-data.local.json');stage='project_initial';projection=initial_projection(payload)
                if s.get('expectedAllyCode') and projection['player']['allyCode']!=s['expectedAllyCode']:raise ValueError('Response account does not match the expected ally code')
                private_write(out,{'capturedAt':datetime.now(timezone.utc).isoformat(),'data':projection})
                result['inventory']=projection['inventory'];result['inbox_count']=len(projection['inbox']);result['shipments']=projection['shipments'];result['guild_available']=projection['guildAvailable']
                path.with_name('initial-wire.private.json').unlink(missing_ok=True)
                result['private_response_saved']=True
            return result
        except Exception as error:
            s['localState']='uncertain';s['lastDiagnostic']={'stage':stage,'response_received':received,'error_type':type(error).__name__};private_write(path,s)
            return {'operation':operation,'success':False,'outcome':'uncertain','retry_allowed':False,**s['lastDiagnostic']}
        finally:
            if own:c.close()
    finally:os.close(lock)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['capture','ping','initial-data','session-check']);p.add_argument('--phase',choices=['initial','ea-app-closed'],default='initial');p.add_argument('--retry-read-only-initial',action='store_true');a=p.parse_args()
    try:
        if a.action=='capture':result=capture(json.loads((STATE.parent/'bridge.local.json').read_text()))
        else:result=run('Ping' if a.action=='ping' else 'IsSessionActive' if a.action=='session-check' else 'GetInitialData',allow_read_retry=a.retry_read_only_initial,phase=a.phase)
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({'success':False,'error_type':type(e).__name__,'message':str(e) if isinstance(e,ValueError) else 'Local session operation unavailable'}))
        raise SystemExit(1)
if __name__=='__main__':main()
