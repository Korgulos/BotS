"""Explicitly published guild TB table. No identities, tokens, commands or write routes."""
import os,re
from pathlib import Path
from uuid import UUID
from fastapi import HTTPException
from fastapi.responses import FileResponse,JSONResponse
from sqlalchemy import select
from . import db

CODE=re.compile(r'^[A-Za-z0-9_:./-]{1,160}$')
NUMBER=re.compile(r'^-?\d{1,20}$')

def public_account():
    try:return str(UUID(os.environ.get('SWGOH_PUBLIC_TB_ACCOUNT','')))
    except ValueError:raise HTTPException(404,'Public TB table is not enabled')

def number(value):return str(value) if value is not None and NUMBER.fullmatch(str(value)) else None

def projection(p):
    """Build a fresh allowlisted document; never serialize raw event metadata."""
    columns=[c for c in p.get('columns',[])[:256] if isinstance(c,str) and CODE.fullmatch(c)]
    rows=[]
    numeric=['galacticPower','territoryPoints','deployedGP','undeployedGP','waves','combatAttempts','donatedUnits']
    for i,r in enumerate(p.get('rows',[])[:100]):
        row={'memberKey':'row-'+str(i+1),'name':str(r.get('name') or 'Član bez imena')[:120],**{k:number(r.get(k)) for k in numeric}}
        row.update({k:r.get(k) if type(r.get(k)) is bool else None for k in ['played','deployed','donated']})
        row['values']={c:number(r.get('values',{}).get(c)) for c in columns}
        row['missions']=[{'zone':str(m.get('zone') or '')[:160],'result':str(m.get('result') or '')[:80],**{k:m.get(k) if type(m.get(k)) is bool else None for k in ['attempted','completed']}} for m in r.get('missions',[])[:256]]
        rows.append(row)
    return {'payload':{'battleKey':str(p.get('battleKey',''))[:32],'definition':str(p.get('definition',''))[:160],
        'phase':p.get('phase'),'state':p.get('state'),'truncated':bool(p.get('truncated')),'columns':columns,'rows':rows,
        **{k:p.get(k) for k in ['statsObservedAt','rosterObservedAt','observedAt']},
        **{k:number(p.get(k)) for k in ['battleStart','phaseEnd']}}}

def install(app,engine):
    panel=Path(__file__).with_name('panel')
    @app.get('/tb',include_in_schema=False)
    def page():
        public_account()
        return FileResponse(panel/'public-tb.html',headers={'X-Robots-Tag':'noindex, nofollow','Referrer-Policy':'no-referrer'})
    @app.get('/public/tb/phases',include_in_schema=False)
    def phases():
        account=public_account()
        q=select(db.events.c.payload).where(db.events.c.account_id==account,db.events.c.service=='Inspector',db.events.c.operation=='TerritoryPhase').order_by(db.events.c.observed_at.desc(),db.events.c.id).limit(60)
        with engine.connect() as c:records=[projection(p) for p in c.execute(q).scalars()]
        return JSONResponse(records,headers={'Cache-Control':'public, max-age=30','X-Robots-Tag':'noindex, nofollow'})
