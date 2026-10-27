"""One local timer tick: wake Render, run the approved maintenance workflow, exit."""
import argparse,fcntl,json,os,stat,uuid
from datetime import datetime,timezone
from pathlib import Path
from urllib.parse import urlparse
import httpx
from .local_session import private_read,private_write,game_running

def tick(config_path,client_factory=httpx.Client,running=game_running):
    cfg=private_read(config_path);url=cfg['cloud_url'].rstrip('/');parts=urlparse(url)
    if parts.scheme!='https' or parts.hostname!='swgoh-inspector-cloud.onrender.com' or parts.path or parts.query or parts.fragment:raise ValueError('Unexpected cloud destination')
    folder=Path(config_path).parent;lock=os.open(folder/'maintenance.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return {'state':'already_running'}
        if running():return {'state':'game_open','sent':False}
        current=datetime.now(timezone.utc);request_id=str(uuid.uuid5(uuid.NAMESPACE_URL,cfg['account_id']+':maintenance:'+current.strftime('%Y-%m-%dT%H')))
        with client_factory(timeout=240,follow_redirects=False,trust_env=False) as c:
            r=c.post(url+'/v1/maintenance/run',headers={'Authorization':'Bearer '+cfg['control_token']},json={'request_id':request_id})
            if r.status_code!=200:
                result={'state':'http_error','status':r.status_code,'at':current.isoformat()}
            else:
                d=r.json();result={k:d.get(k) for k in ('state','tb','inbox','energy','refreshed','code') if k in d};result['at']=current.isoformat()
        private_write(folder/'maintenance-last.local.json',result);return result
    finally:os.close(lock)

def main():
    p=argparse.ArgumentParser();p.add_argument('--config',required=True);args=p.parse_args()
    try:
        result=tick(args.config);print(json.dumps(result,ensure_ascii=False))
        if result.get('state') in {'failed','http_error','session_unavailable','uncertain'}:raise SystemExit(1)
    except (ValueError,OSError,httpx.HTTPError) as e:print(json.dumps({'state':'failed','error_type':type(e).__name__}));raise SystemExit(1)
if __name__=='__main__':main()
