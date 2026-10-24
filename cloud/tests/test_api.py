from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from sqlalchemy import select, func
from swgoh_cloud import db
from swgoh_cloud.api import create_app
from swgoh_cloud.config import Settings

ADMIN="A"*48

def setup(engine):
    client=TestClient(create_app(Settings("unused",ADMIN,max_body_bytes=2*1024*1024),engine))
    admin={"Authorization":"Bearer "+ADMIN}
    account=client.post("/v1/accounts",headers=admin,json={"alias":"main"}).json()
    source=client.post(f'/v1/accounts/{account["account_id"]}/sources',headers=admin,json={"name":"home"}).json()
    return client,admin,account,source

def observation(**changes):
    data=dict(session="inspector-session-001",event_key="snapshot:shipments:1",revision=1,
        observed_at=datetime.now(timezone.utc).isoformat(),service="Inspector",operation="Snapshot",topic="shipments",metadata={},
        payload={"available":True,"offers":{"kind":"shipments","shipments":[{"type":"SHIPMENTPVE","nextRefresh":"1792882800","items":[{"itemId":"OFFER1","purchased":False,"cost":{"definition":"GRIND","minQuantity":2700},"reward":{"definition":"GEAR","minQuantity":6}}]}]}})
    data.update(changes);return data

def test_auth_isolation_and_ingestion(engine):
    c,admin,a,s=setup(engine);writer={"Authorization":"Bearer "+s["ingest_token"]};reader={"Authorization":"Bearer "+a["read_token"]}
    assert c.get("/v1/accounts",headers={"Authorization":"Bearer ččč".encode()}).status_code==401
    assert c.get("/v1/events").status_code==401
    assert c.get("/v1/events",headers=writer).status_code==403
    assert c.post("/v1/ingest",headers=reader,json={"events":[observation()]}).status_code==403
    e=observation();response=c.post("/v1/ingest",headers=writer,json={"events":[e]});assert response.status_code==200,response.text
    assert response.json()["acknowledged"][0]["state"]=="inserted"
    assert c.post("/v1/ingest",headers=writer,json={"events":[e]}).json()["acknowledged"][0]["state"]=="duplicate"
    assert len(c.get("/v1/events",headers=reader).json())==1
    assert c.get("/v1/shipments",headers=reader).json()[0]["offer_id"]=="OFFER1"
    listed=c.get("/v1/events",headers=reader).json()[0]
    assert "payload" not in listed
    assert c.get("/v1/events/"+listed["id"],headers=reader).json()["payload"]["available"]
    assert c.get("/v1/snapshots",headers=reader).json()[0]["payload"]["available"]
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.offers)).scalar()==1
        rows=conn.execute(select(db.keys)).mappings().all();assert all(r["digest"]!=s["ingest_token"] for r in rows)
    other=c.post("/v1/accounts",headers=admin,json={"alias":"other"}).json()
    assert c.get("/v1/events",headers={"Authorization":"Bearer "+other["read_token"]}).json()==[]
    assert c.get("/v1/events/"+listed["id"],headers={"Authorization":"Bearer "+other["read_token"]}).status_code==404
    key=next(r for r in c.get("/v1/keys",headers=admin).json() if r["role"]=="ingest")
    assert "digest" not in key
    assert c.delete('/v1/keys/'+key["id"],headers=admin).status_code==200
    assert c.post("/v1/ingest",headers=writer,json={"events":[e]}).status_code==401
    assert not c.get("/v1/capabilities",headers=reader).json()["standalone_game_rpc"]


def test_revision_atomicity_and_snapshot_order(engine):
    c,admin,a,s=setup(engine);w={"Authorization":"Bearer "+s["ingest_token"]};r={"Authorization":"Bearer "+a["read_token"]}
    first=observation();assert c.post('/v1/ingest',headers=w,json={"events":[first]}).status_code==200
    changed={**first,"metadata":{"status":201}}
    fresh=observation(event_key="fresh")
    assert c.post('/v1/ingest',headers=w,json={"events":[fresh,changed]}).status_code==409
    assert len(c.get('/v1/events',headers=r).json())==1  # Entire batch rolled back.
    newer={**changed,"revision":2};assert c.post('/v1/ingest',headers=w,json={"events":[newer]}).json()["acknowledged"][0]["state"]=="updated"
    assert c.post('/v1/ingest',headers=w,json={"events":[first]}).json()["acknowledged"][0]["state"]=="stale"
    old=observation(event_key="older",observed_at=(datetime.now(timezone.utc)-timedelta(days=1)).isoformat(),payload={"old":True})
    assert c.post('/v1/ingest',headers=w,json={"events":[old]}).status_code==200
    assert "old" not in c.get('/v1/snapshots',headers=r).json()[0]["payload"]


def test_reject_secrets_limits_and_bad_inputs(engine):
    c,admin,a,s=setup(engine);w={"Authorization":"Bearer "+s["ingest_token"]}
    for secret in ["identityToken","Authorization","rawData","requestEnvelope","claimReference"]:
        response=c.post('/v1/ingest',headers=w,json={"events":[observation(payload={secret:"DO_NOT_ECHO"})]})
        assert response.status_code==422
        assert "DO_NOT_ECHO" not in response.text
    for changes in [dict(service="AuthRpc",operation="DoAuthNucleus"),dict(observed_at="2039-01-01T00:00:00Z"),dict(observed_at="2026-01-01T00:00:00"),dict(payload={"large":"a"*5000})]:
        assert c.post('/v1/ingest',headers=w,json={"events":[observation(**changes)]}).status_code==422
    assert c.post('/v1/ingest',headers=w,json={"events":[{**observation(),"account_id":a["account_id"]}]}).status_code==422
    assert c.post('/v1/ingest',headers=w,content=b'x'*(2*1024*1024+1)).status_code==413


def test_concurrent_idempotency(engine):
    c,admin,a,s=setup(engine);w={"Authorization":"Bearer "+s["ingest_token"]};e=observation()
    # Don't use lifespan contexts per thread (they dispose the shared pool).
    def send_one(_):
        peer=TestClient(create_app(Settings("unused",ADMIN),engine))
        return peer.post('/v1/ingest',headers=w,json={"events":[e]}).status_code
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(send_one,range(8)))==[200]*8
    with engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(db.events)).scalar()==1
        assert conn.execute(select(func.count()).select_from(db.offers)).scalar()==1


def test_battle_and_guild_projection(engine):
    c,admin,a,s=setup(engine);w={"Authorization":"Bearer "+s["ingest_token"]};r={"Authorization":"Bearer "+a["read_token"]}
    battle=observation(event_key="rpc:12",service="BattleRpc",operation="BattleFinish",topic=None,payload={"kind":"battle","phase":"finish","request":{"type":"TERRITORYWARBATTLE","outcome":"WIN","recordCount":435,"records":[{"index":1}],"recordsTruncated":True,"state":{"data":{"battleSeed":-900919326}}}})
    guild=observation(event_key="rpc:13",service="GuildRpc",operation="GetGuild",topic="guild",payload={"members":[{"name":"Member","galacticPower":"999999999999999999","level":85}]})
    assert c.post('/v1/ingest',headers=w,json={"events":[battle,guild]}).status_code==200
    record=c.get('/v1/battles',headers=r).json()[0]
    assert c.get("/v1/guild/members",headers=r).json()[0]["name"]=="Member"
    assert record["seed"]=="-900919326" and record["truncated"] and record["records_shown"]==1
    with engine.connect() as conn:
        assert conn.execute(select(db.members.c.galactic_power)).scalar()=="999999999999999999"
