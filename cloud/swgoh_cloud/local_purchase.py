"""Manual local direct purchase trial: explicit quote approval, Credits only, no retries."""
import fcntl,os,time,uuid,json,argparse,zlib
from datetime import datetime,timezone
import httpx
from .local_session import STATE,private_read,private_write,game_running
from .shipment_wire import offers,purchase_packet,fields,one,child,receive
from .session_wire import build_request,LIMIT,initial_projection
from .direct_probe import RPC_URL
JOURNAL=STATE.with_name('direct-purchase.local.json')


def prepare(item_id,path=STATE,journal=JOURNAL,expected_ally_code=None):
    s=private_read(path);snapshot=private_read(path.with_name('initial-data.local.json'))
    if type(expected_ally_code) is not int or snapshot['data']['player']['allyCode']!=expected_ally_code:raise ValueError('Explicit expected account must match the direct snapshot')
    s['expectedAllyCode']=expected_ally_code
    private_write(path,s)
    age=(datetime.now(timezone.utc)-datetime.fromisoformat(snapshot['capturedAt'])).total_seconds()
    if not 0<=age<120:raise ValueError('Fresh direct initial data required')
    if journal.exists():raise ValueError('Previous direct purchase journal exists; review it first')
    matches=[q for q in snapshot['data']['creditOffers'] if q['item_id']==item_id]
    if len(matches)!=1:raise ValueError('Offer not uniquely identified')
    q=matches[0]
    if q.get('currency')!='GRIND':raise ValueError('Manual local trial remains Credits only; use the cloud panel for other currencies')
    purchase_packet(s,q)
    doc={'id':str(uuid.uuid4()),'quote':q,'state':'prepared','expires':time.time()+120,'sessionCapturedAt':s['capturedAt']}
    private_write(journal,doc);return doc

def commit(identifier,confirmed=False,path=STATE,journal=JOURNAL,client=None,running=game_running):
    if not confirmed:raise ValueError('Explicit quote confirmation required')
    if running():raise ValueError('Close SWGoH before direct purchase')
    fd=os.open(path.with_name(path.name+'.lock'),os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        doc=private_read(journal);s=private_read(path)
        if doc['id']!=identifier or doc['state']!='prepared' or doc['expires']<=time.time():raise ValueError('Approval expired or already attempted')
        age=(datetime.now(timezone.utc)-datetime.fromisoformat(s['capturedAt'])).total_seconds()
        if not 0<=age<=600 or s['capturedAt']!=doc['sessionCapturedAt'] or s.get('localState')!='ready' or s.get('requestTokenMode')!='absent' or s.get('requestToken'):raise ValueError('Fresh verified nonsequenced session required')
        doc['state']='checking';private_write(journal,doc)
        own=client is None;c=client or httpx.Client(timeout=20,trust_env=False,follow_redirects=False)
        sending=False
        try:
            code,payload=receive(c,build_request(s,'GetInitialData'))
            if code!=1 or initial_projection(payload)['player']['allyCode']!=s.get('expectedAllyCode'):raise ValueError('Account verification failed')
            candidates=[q for q in offers(payload) if q['item_id']==doc['quote']['item_id'] and q['shipment_type']==doc['quote']['shipment_type']]
            if len(candidates)!=1:raise ValueError('Offer no longer available')
            fresh=candidates[0]
            for key in ('item_id','shipment_type','definition','quantity','price','currency','refresh'):
                if fresh[key]!=doc['quote'][key]:raise ValueError('Offer changed; purchase not sent')
            packet=purchase_packet(s,fresh)
            doc['state']='sending';private_write(journal,doc)
            s['localState']='sending';private_write(path,s);sending=True
            code,payload=receive(c,packet)
            if code!=1:
                doc.update(state='rejected',rpc_code=code);s['localState']='rejected'
            else:
                delta=child(fields(payload),1);debit=child(delta,2);costs=[]
                for blob in debit.get(1,[]):
                    n=fields(blob);costs.append({'currency':one(n,1,1),'quantity':one(n,2,0)})
                if costs!=[{'currency':1,'quantity':fresh['price']}]:raise ValueError('Successful response debit differs; review outcome')
                doc.update(state='succeeded',rpc_code=code,debit=costs);s['localState']='purchase_complete'
            private_write(path,s);private_write(journal,doc)
            return {'state':doc['state'],'rpc_code':code,'quote':doc['quote'],'debit':doc.get('debit',[]),'retry_allowed':False}
        except Exception as e:
            doc.update(state='uncertain' if sending else 'blocked',error_type=type(e).__name__)
            private_write(journal,doc)
            if sending:s['localState']='uncertain';private_write(path,s)
            return {'state':doc['state'],'purchase_sent':sending,'retry_allowed':False,'error_type':type(e).__name__}
        finally:
            if own:c.close()
    finally:os.close(fd)

def main():
    p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','commit']);p.add_argument('identifier');p.add_argument('--confirmed',action='store_true');p.add_argument('--ally-code',type=int);a=p.parse_args()
    try:print(json.dumps(prepare(a.identifier,expected_ally_code=a.ally_code) if a.action=='prepare' else commit(a.identifier,a.confirmed)))
    except Exception as e:print(json.dumps({'success':False,'error_type':type(e).__name__,'message':str(e) if isinstance(e,ValueError) else 'Unavailable'}));raise SystemExit(1)
if __name__=='__main__':main()
