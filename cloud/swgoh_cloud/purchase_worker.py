"""Local, crash-safe shipment executor. Never repeats a native commit POST."""
from datetime import datetime, timezone
import json
import re
import httpx
from .purchases import fingerprint, ALLOWED
from .inbox_policy import message_hash, preview, native_preview

class PurchaseWorker:
    def __init__(self, conn):
        self.conn=conn
        conn.execute('PRAGMA synchronous=FULL')
        conn.execute('CREATE TABLE IF NOT EXISTS purchase_journal (id TEXT PRIMARY KEY, session TEXT NOT NULL, state TEXT NOT NULL, document TEXT NOT NULL)')
        conn.commit()
    def load(self,identifier):
        row=self.conn.execute('SELECT session,state,document FROM purchase_journal WHERE id=?',(identifier,)).fetchone()
        return dict(session=row[0],state=row[1],document=json.loads(row[2])) if row else None
    def save(self,identifier,session,state,doc):
        with self.conn:
            self.conn.execute('INSERT INTO purchase_journal VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET session=excluded.session,state=excluded.state,document=excluded.document',(identifier,session,state,json.dumps(doc)))
    def tick(self,client,cloud,ingest,base,key,session,online,enabled):
        headers={'Authorization':'Bearer '+ingest}
        def cloud_post(path,data):
            r=client.post(cloud+path,headers=headers,json=data);r.raise_for_status();return r.json()
        cloud_post('/v1/bridge/heartbeat',dict(session=session,online=online,purchases_enabled=enabled))
        if not online or not enabled:return
        response=client.get(cloud+'/v1/bridge/purchases',headers=headers);response.raise_for_status();command=response.json()
        if not command:return
        identifier=command['id'];phase=command['state'];local=self.load(identifier)
        inbox=command['expected'].get('kind')=='inbox'
        route='/api/inbox/claim' if inbox else '/api/purchase'
        def report(state,code='',**extra):
            return cloud_post('/v1/bridge/purchases/'+identifier+'/report',dict(session=command['session'],state=state,code=re.sub('[^A-Za-z0-9_. -]','_',str(code))[:160],**extra))
        if command['session']!=session:
            report('uncertain' if phase in {'sending','uncertain'} else 'failed','GAME_SESSION_CHANGED');return
        if local and local['session']!=session:
            report('uncertain' if phase in {'sending','uncertain'} else 'failed','LOCAL_SESSION_CHANGED');return
        def native(path,data=None):
            h={'X-Inspector-Key':key,'Origin':base}
            r=client.get(base+path,headers=h) if data is None else client.post(base+path,headers=h,json=data)
            r.raise_for_status();return r.json()
        if local and local['state']=='settled':
            report(local['document']['result']['state'],local['document']['result']['code']);return
        if phase in {'queued_prepare','preparing'}:
            expected=command['expected']
            if not inbox and expected.get('currency') not in ALLOWED:
                report('failed','CURRENCY_BLOCKED');return
            cloud_post('/v1/bridge/purchases/'+identifier+'/claim',dict(session=session,phase='prepare'))
            if local and local['state']=='prepared':doc=local['document']
            else:
                try:
                    if inbox:
                        snapshot=native('/api/inbox')
                        matches=[m for m in snapshot.get('inbox',{}).get('messages',[]) if message_hash(m)==expected['message_hash']]
                        if len(matches)!=1 or preview(matches[0])!=expected['preview']:raise ValueError('Inbox snapshot changed')
                        reference=matches[0].get('claimReference')
                        if not isinstance(reference,str) or not re.fullmatch('[a-fA-F0-9]{48}',reference):raise ValueError('Missing native reference')
                        doc=native(route+'/prepare',{'reference':reference})
                        doc['quote']=native_preview(doc['quote'])
                    else:doc=native(route+'/prepare',expected)
                    if doc.get('error') or not re.fullmatch('[a-fA-F0-9]{48}',doc.get('token','')) or (doc['quote']!=expected['preview'] if inbox else any(doc['quote'].get(k)!=v for k,v in expected.items())):
                        report('failed','LIVE_OFFER_VALIDATION_FAILED');return
                    self.save(identifier,session,'prepared',doc)
                except (httpx.HTTPError,ValueError,KeyError,TypeError):
                    report('failed','PREPARE_UNAVAILABLE');return
            report('prepared','READY',quote=doc['quote'],expires_at=doc['expiresAt']);return
        if phase=='prepared':return  # No commit until a separate human confirmation.
        if phase not in {'queued_commit','sending','uncertain'}:return
        if not local:
            report('uncertain','LOCAL_JOURNAL_MISSING');return
        doc=local['document']
        if command.get('quote_hash')!=fingerprint(doc.get('quote')) or command.get('confirmed_at') is None:
            report('uncertain','APPROVAL_MISMATCH');return
        if local['state'] in {'prepared','commit_reserved'}:
            if phase=='uncertain':return  # A timed-out claim is never sent later.
            if local['state']=='prepared' and phase!='queued_commit':
                report('uncertain','UNRECORDED_COMMIT_CLAIM');return
            if datetime.fromisoformat(doc['expiresAt'])<=datetime.now(timezone.utc):
                report('failed','NATIVE_QUOTE_EXPIRED');return
            self.save(identifier,session,'commit_reserved',doc)
            cloud_post('/v1/bridge/purchases/'+identifier+'/claim',dict(session=session,phase='commit'))
            # Synchronous SQLite commit BEFORE the only native mutation that spends resources.
            self.save(identifier,session,'committing',doc)
            try:result=native(route+'/commit',{'token':doc['token'],'confirmed':True})
            except (httpx.HTTPError,ValueError):result={'state':'uncertain','code':'SEND_RESPONSE_UNAVAILABLE'}
        else:
            # Recovery and all subsequent checks use GET. Never replay the native commit POST.
            try:result=native(route+'/status?token='+doc['token'])
            except (httpx.HTTPError,ValueError):result={'state':'uncertain','code':'STATUS_UNAVAILABLE'}
        state=result.get('state')
        if state=='sending':return
        if state not in {'succeeded','failed'}:state='uncertain'
        code=result.get('code') or 'OUTCOME_UNCONFIRMED'
        if state in {'succeeded','failed'}:
            doc['result']=dict(state=state,code=code)
            self.save(identifier,session,'settled',doc)
        report(state,code)
