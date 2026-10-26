"""Allowlisted, runtime-schema-verified read-only Territory Battle codec."""
import hashlib,json,re
from pathlib import Path
from .shipment_wire import fields,one,receive
from .session_wire import build_request,initial_projection
from .direct_wire import field

SCHEMA=Path(__file__).with_name('territory_schema.json')
ALLOWED={'GetGuild':('GuildRpc','GetGuildRequest','GetGuildResponse'),'GetTerritoryMapStatuses':('TerritoryRpc','GetTerritoryMapStatusesRequest','GetTerritoryMapStatusesResponse'),'GetMapStats':('TerritoryRpc','GetMapStatsRequest','GetMapStatsResponse')}

def key(value):return hashlib.sha256(('swgoh-territory:'+value).encode()).hexdigest()[:32] if value else ''
def clean(value):return ''.join(c for c in value if c.isprintable())[:120]
class Codec:
    def __init__(self,schema=None):self.schema=schema or json.loads(SCHEMA.read_text())
    def tag(self,typ,name):return self.schema[typ][name]['tag']
    def values(self,blob,typ,name):return fields(blob).get(self.tag(typ,name),[])
    def scalar(self,blob,typ,name,default=None):
        values=self.values(blob,typ,name)
        if len(values)>1:raise ValueError('Duplicate territory scalar')
        value=values[0] if values else default
        kind=self.schema[typ][name]['type']
        if kind=='string':
            if value is None:return ''
            if not isinstance(value,bytes):raise ValueError('Invalid territory string')
            return value.decode('utf8')
        if kind in {'int32','int64'} and isinstance(value,int) and value>=2**63:value-=2**64
        return value
    def children(self,blob,typ,name,cap=256):
        values=self.values(blob,typ,name)
        if len(values)>cap:raise ValueError('Territory collection exceeds cap')
        if any(not isinstance(v,bytes) for v in values):raise ValueError('Invalid territory message')
        return values
    def child(self,blob,typ,name):
        values=self.children(blob,typ,name,1);return values[0] if values else b''
    def packet(self,session,operation,body=None):
        service,request,_=ALLOWED[operation]
        payload=b''
        for name,value in (body or {}).items():payload+=field(self.tag(request,name),value)
        envelope=fields(build_request(session,'Ping'));envelope[4]=[service.encode()];envelope[5]=[operation.encode()];envelope[6]=[payload]
        return b''.join(field(tag,v) for tag,values in envelope.items() for v in values)
    def call(self,client,session,operation,body=None):
        code,payload=receive(client,self.packet(session,operation,body))
        if code!=1:raise SessionRejected()
        return payload
    def members(self,guild):
        out=[]
        for m in self.children(guild,'Guild','member',100):
            s=lambda n,d=None:self.scalar(m,'GuildMember',n,d)
            out.append(dict(memberKey=key(s('player_id')),name=clean(s('player_name')),galacticPower=str(s('galactic_power',0)),characterPower=str(s('character_galactic_power',0)),shipPower=str(s('ship_galactic_power',0))))
        return out
    def stats(self,blobs,names):
        out=[]
        for blob in blobs:
            code=self.scalar(blob,'TerritoryMapStat','map_stat_id')
            if not re.fullmatch(r'[A-Za-z0-9_:./-]{1,160}',code):continue
            players=[]
            for p in self.children(blob,'TerritoryMapStat','player_stat',100):
                member=key(self.scalar(p,'TerritoryPlayerStat','member_id'));covert=[]
                for c in self.children(p,'TerritoryPlayerStat','covert_zone_result',30):
                    result=self.scalar(c,'TerritoryStatCovertZoneResult','result',0)
                    covert.append(dict(zone=self.scalar(c,'TerritoryStatCovertZoneResult','covert_zone_id'),result={0:'UNATTEMPTED',1:'ATTEMPTEDFAILED',2:'ATTEMPTEDSUCCEEDED'}.get(result,'UNKNOWN')))
                players.append(dict(memberKey=member,member=names.get(member,'Član bez imena'),score=str(self.scalar(p,'TerritoryPlayerStat','score',0)),covert=dict(items=covert,count=len(covert),truncated=False)))
            out.append(dict(statId=code,players=dict(items=players,count=len(players),truncated=False)))
        return dict(items=out,count=len(out),truncated=False)
    def run(self,blob,ended,names,now):
        t='TerritoryBattleResult' if ended else 'TerritoryBattleStatus';s=lambda n,d=None:self.scalar(blob,t,n,d)
        run=dict(battleKey=key(s('instance_id')),definition=s('definition_id'),currentRound=6 if ended else s('current_round',0),roundEnd=None if ended else str(s('current_round_end_time',0)),ended=ended,statusObservedAt=now,statsObservedAt=now,statsRound=6 if ended else s('current_round',0),statsFinal=ended)
        if ended:run['battleStart']=str(s('start_time',0))
        run['stats']=self.stats(self.children(blob,t,'final_stat' if ended else 'current_stat'),names)
        return run

def collect(session,client,now,codec=None):
    c=codec or Codec()
    code,initial=receive(client,build_request(session,'GetInitialData'))
    if code!=1:raise SessionRejected()
    if initial_projection(initial)['player']['allyCode']!=session['expectedAllyCode']:raise ValueError('Account mismatch')
    # Discover only this authenticated account's guild; never take a map ID from the browser.
    guild=c.child(initial,'InitialDataResponse','guild')
    profile=c.child(guild,'Guild','profile');guild_id=c.scalar(profile,'GuildProfile','id')
    if not guild_id:raise ValueError('Account has no guild')
    response=c.call(client,session,'GetGuild',{'guild_id':guild_id})
    guild=c.child(response,'GetGuildResponse','guild')
    actual_id=c.scalar(c.child(guild,'Guild','profile'),'GuildProfile','id')
    if actual_id!=guild_id:raise ValueError('Guild mismatch')
    members=c.members(guild);names={m['memberKey']:m['name'] for m in members}
    if not members:raise ValueError('Empty guild roster')
    statuses=c.call(client,session,'GetTerritoryMapStatuses',{'event_type':9})
    runs=[]
    for ended,fieldname in [(False,'territory_battle_status'),(True,'territory_battle_result')]:
        for b in c.children(statuses,'GetTerritoryMapStatusesResponse',fieldname,10):
            run=c.run(b,ended,names,now)
            if not ended and run['currentRound'] in range(1,7):
                map_id=c.scalar(b,'TerritoryBattleStatus','instance_id')
                stats=c.call(client,session,'GetMapStats',{'territory_map_id':map_id,'event_type':9})
                run['stats']=c.stats(c.children(stats,'GetMapStatsResponse','current_stat'),names)
            if run['currentRound'] in range(1,7):runs.append(run)
    return dict(available=True,observedAt=now,rosterObservedAt=now,members=members,runs=runs)

class SessionRejected(Exception):pass
