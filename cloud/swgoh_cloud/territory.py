"""Read-only TB projections and six phase checkpoints in the existing SQL event store."""
from datetime import datetime, timezone
import json
import re
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from . import db

KEY = re.compile(r'^[0-9a-f]{32}$')
ROUND = re.compile(r'_round_([1-6])$')
ZONE = re.compile(r'_phase0?([1-6])_')
RESULTS = {'UNATTEMPTED': (False, False), 'ATTEMPTEDFAILED': (True, False), 'ATTEMPTEDSUCCEEDED': (True, True)}

def integer(value):
    try:
        if type(value) is bool: return None
        text = str(value)
        return int(text) if re.fullmatch(r'-?\d{1,20}', text) else None
    except (ValueError, TypeError): return None

def stamp(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except (ValueError, TypeError): return None

def items(collection):
    return collection.get('items', []) if isinstance(collection, dict) and isinstance(collection.get('items'), list) else []

def phase_view(snapshot, run, phase):
    """Missing player/stat entries stay null, never silently become zero or false."""
    members = snapshot.get('members') if isinstance(snapshot.get('members'), list) else []
    rows = {}
    for member in members[:100]:
        if not isinstance(member, dict) or not KEY.fullmatch(str(member.get('memberKey', ''))): continue
        key = member['memberKey']
        rows[key] = dict(memberKey=key, name=member.get('name'), galacticPower=member.get('galacticPower'),
                         characterPower=member.get('characterPower'), shipPower=member.get('shipPower'), role=member.get('role'), values={}, missions=[])
    columns = []
    stats = run.get('stats')
    truncated = bool(isinstance(stats, dict) and stats.get('truncated'))
    for stat in items(stats)[:256]:
        if not isinstance(stat, dict): continue
        code = str(stat.get('statId', ''))
        round_match, zone_match = ROUND.search(code), ZONE.search(code)
        if not ((round_match and int(round_match[1]) == phase) or (zone_match and int(zone_match[1]) == phase)): continue
        columns.append(code)
        players = stat.get('players') if isinstance(stat.get('players'), dict) else {}
        truncated |= bool(players.get('truncated'))
        for player in items(players)[:100]:
            if not isinstance(player, dict): continue
            key = player.get('memberKey')
            if not KEY.fullmatch(str(key or '')): continue
            row = rows.setdefault(key, dict(memberKey=key, name=player.get('member'), galacticPower=None, characterPower=None, shipPower=None, role=None, values={}, missions=[]))
            row['values'][code] = str(player['score']) if integer(player.get('score')) is not None else None
            covert = player.get('covert') if isinstance(player.get('covert'), dict) else {}
            truncated |= bool(covert.get('truncated'))
            for result in items(covert)[:30]:
                if not isinstance(result, dict): continue
                state = result.get('result')
                attempted, completed = RESULTS.get(state, (None, None))
                mission = dict(zone=result.get('zone'), result=state, attempted=attempted, completed=completed)
                if mission not in row['missions']: row['missions'].append(mission)
    suffix = '_round_' + str(phase)
    for row in rows.values():
        values = row['values']
        def value(code): return integer(values.get(code + suffix))
        row['territoryPoints'] = values.get('summary' + suffix)
        row['deployedGP'] = values.get('power' + suffix)
        gp, deployed = integer(row['galacticPower']), value('power')
        # Live roster GP is not proof of the TB-locked eligible roster; retain provenance.
        row['undeployedGP'] = str(gp - deployed) if gp is not None and deployed is not None and gp >= deployed else None
        row['undeployedDerived'] = row['undeployedGP'] is not None
        row['waves'] = values.get('strike_encounter' + suffix)
        row['combatAttempts'] = values.get('strike_attempt' + suffix)
        row['donatedUnits'] = values.get('unit_donated' + suffix)
        row['deployed'] = (deployed > 0) if deployed is not None else None
        donated = value('unit_donated')
        row['donated'] = (donated > 0) if donated is not None else None
        attempts, waves = value('strike_attempt'), value('strike_encounter')
        row['played'] = True if (attempts or 0) > 0 or (waves or 0) > 0 or any(x['attempted'] is True for x in row['missions']) else (False if attempts == 0 else None)
    return dict(phase=phase, rows=list(rows.values()), columns=list(dict.fromkeys(columns)), truncated=truncated,
                rosterObservedAt=snapshot.get('rosterObservedAt'), statsObservedAt=run.get('statsObservedAt'),
                units='raw server statistics; undeployedGP is derived from the captured guild roster')

def checkpoint(conn, event):
    payload = event.payload or {}
    if not payload.get('available') or not isinstance(payload.get('runs'), list): return
    # Serialize phase finalization across concurrent ingestion batches for this account.
    conn.execute(select(db.accounts.c.id).where(db.accounts.c.id == event.account_id).with_for_update())
    for run in payload['runs'][:8]:
        if not isinstance(run, dict): continue
        run_key = str(run.get('battleKey', ''))
        current, stat_round = integer(run.get('currentRound')), integer(run.get('statsRound'))
        if not KEY.fullmatch(run_key) or current not in range(1, 7): continue
        stats_at, status_at = stamp(run.get('statsObservedAt')), stamp(run.get('statusObservedAt'))
        for phase in range(1, current + 1):
            session, event_key = 'tb-phase-' + run_key, 'phase:' + str(phase)
            old = conn.execute(select(db.events).where(db.events.c.source_id == event.source_id,
                db.events.c.session == session, db.events.c.event_key == event_key).with_for_update()).mappings().first()
            baseline = payload
            if old and phase < current and old['payload'].get('rows'):
                latest_members={m.get('memberKey'):m for m in payload.get('members',[]) if isinstance(m,dict)}
                historical=[]
                for member in old['payload']['rows']:
                    member=dict(member)
                    if not member.get('name'):
                        fresh=latest_members.get(member.get('memberKey'),{})
                        member['name']=fresh.get('name')
                        # Older partial GetGuild replies supplied anonymous zero GP;
                        # preserve real historical GP, but do not treat these placeholders as zero.
                        if member.get('galacticPower')=='0':member['galacticPower']=None
                    historical.append(member)
                baseline = dict(payload, members=historical, rosterObservedAt=old['payload'].get('rosterObservedAt'))
            projected = phase_view(baseline, run, phase)
            # A later-phase server response includes explicit historical _round_N values.
            has_stats = bool(projected['columns']) and stats_at is not None
            final = has_stats and (bool(run.get('statsFinal') and run.get('ended')) or (stat_round is not None and stat_round > phase))
            state = 'final' if final else ('closed_last_seen' if phase < current or run.get('ended') else 'live')
            captured = stats_at if has_stats else status_at
            if captured is None: continue
            if old:
                previous = old['payload'] or {}
                if previous.get('state') == 'final' and not final: continue
                old_stats = stamp(previous.get('statsObservedAt'))
                if old_stats and (stats_at is None or stats_at < old_stats): continue
                if not has_stats:
                    projected = dict(previous)
                    if previous.get('state') != 'final': projected['state'] = state
            elif not has_stats:
                # A map/roster snapshot alone cannot invent final contribution numbers.
                if phase < current: projected['rows'] = []
            projected.update(battleKey=run_key, definition=run.get('definition'), phase=phase, state=state,
                             observedAt=captured.isoformat(), battleStart=run.get('battleStart') or (old['payload'].get('battleStart') if old else None), phaseEnd=run.get('roundEnd') if phase == current else (old['payload'].get('phaseEnd') if old else None))
            raw = json.dumps(projected, sort_keys=True, separators=(',', ':'), ensure_ascii=False)
            digest = db.digest(raw)
            if old and old['content_hash'] == digest: continue
            values = dict(id=old['id'] if old else db.uid(), account_id=event.account_id, source_id=event.source_id,
                          session=session, event_key=event_key, revision=(old['revision'] + 1) if old else 1,
                          observed_at=captured, received_at=db.now(), service='Inspector', operation='TerritoryPhase', topic=None,
                          content_hash=digest, metadata={}, payload=projected)
            conn.execute(insert(db.events).values(**values).on_conflict_do_update(index_elements=['source_id','session','event_key'],
                         set_={k:v for k,v in values.items() if k not in {'id','account_id','source_id','session','event_key'}}))

def install(app, engine, reader):
    from fastapi import Depends, Query
    @app.get('/v1/territory/phases')
    def phases(who=Depends(reader), battle: str | None = Query(None, max_length=32), phase: int | None = Query(None, ge=1, le=6)):
        q = select(db.events.c.id, db.events.c.source_id, db.events.c.observed_at, db.events.c.payload).where(
            db.events.c.account_id == who['account_id'], db.events.c.service == 'Inspector', db.events.c.operation == 'TerritoryPhase')
        if battle:
            if not KEY.fullmatch(battle): return []
            q = q.where(db.events.c.session == 'tb-phase-' + battle)
        if phase: q = q.where(db.events.c.event_key == 'phase:' + str(phase))
        with engine.connect() as conn:
            return [dict(r) for r in conn.execute(q.order_by(db.events.c.observed_at.desc(), db.events.c.id).limit(60)).mappings()]
