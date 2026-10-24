"""Read-only local inspector -> durable outbox -> HTTPS cloud ingestion."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlparse

import httpx
from .models import OPERATIONS, SECRET_KEYS, check_tree


def utc():
    return datetime.now(timezone.utc).isoformat()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sanitize(value):
    if isinstance(value, dict):
        import re
        return {k: sanitize(v) for k,v in value.items() if re.sub(r"[^a-z0-9]", "", k.lower()) not in SECRET_KEYS
                and not re.sub(r"[^a-z0-9]", "", k.lower()).endswith("token")}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    return value


def validate_cloud_url(url):
    parsed = urlparse(url)
    local_test = parsed.scheme == "http" and parsed.hostname == "127.0.0.1"
    if (parsed.scheme != "https" and not local_test) or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("Cloud URL must be HTTPS (HTTP only allowed on 127.0.0.1 for testing)")
    return url.rstrip("/")


class Outbox:
    def __init__(self, path):
        path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.conn.executescript('''
          PRAGMA journal_mode=WAL;
          CREATE TABLE IF NOT EXISTS checkpoints (
            session TEXT, name TEXT, digest TEXT, revision INTEGER, observed_at TEXT,
            PRIMARY KEY(session,name));
          CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY AUTOINCREMENT, body TEXT NOT NULL);
        ''')

    def record(self, session, name, service, operation, metadata, payload, topic=None, observed_at=None):
        payload = sanitize(payload); metadata = sanitize(metadata)
        check_tree(payload); check_tree(metadata)
        hashed = hashlib.sha256(canonical([service,operation,topic,metadata,payload]).encode()).hexdigest()
        with self.conn:
            prior = self.conn.execute("SELECT digest,revision,observed_at FROM checkpoints WHERE session=? AND name=?",(session,name)).fetchone()
            if prior and prior[0] == hashed:
                return False
            count, size = self.conn.execute("SELECT count(*),coalesce(sum(length(CAST(body AS BLOB))),0) FROM outbox").fetchone()
            if count >= 5000 or size > 128*1024*1024:
                raise RuntimeError("Outbox full; restore cloud connectivity before collecting more")
            revision = prior[1]+1 if prior else 1
            # RPC rows enrich in place. Snapshot changes are separate immutable events.
            snapshot = service == "Inspector"
            event_key = f"{name}:{revision}" if snapshot else name
            observed = (observed_at or utc()) if snapshot or not prior else prior[2]
            body = dict(session=session,event_key=event_key,revision=1 if snapshot else revision,
                observed_at=observed,service=service,operation=operation,topic=topic,metadata=metadata,payload=payload)
            encoded = canonical(body)
            if len(encoded.encode()) > 1024*1024:
                raise ValueError("Observation exceeds cloud limit")
            self.conn.execute("INSERT INTO outbox(body) VALUES (?)",(encoded,))
            self.conn.execute("INSERT INTO checkpoints VALUES (?,?,?,?,?) ON CONFLICT(session,name) DO UPDATE SET digest=excluded.digest,revision=excluded.revision,observed_at=excluded.observed_at",(session,name,hashed,revision,observed))
        return True

    def flush(self, client, base_url, token):
        rows = self.conn.execute("SELECT id,body FROM outbox ORDER BY id LIMIT 20").fetchall()
        if not rows:
            return 0
        chosen = []; size = 0
        for row in rows:
            if size + len(row[1].encode()) > 1500000:
                break
            chosen.append(row); size += len(row[1].encode())
        response = client.post(base_url+"/v1/ingest", headers={"Authorization":"Bearer "+token},
            json={"events":[json.loads(row[1]) for row in chosen]})
        response.raise_for_status()
        ack = response.json().get("acknowledged",[])
        expected = [(json.loads(row[1])["event_key"],json.loads(row[1])["revision"]) for row in chosen]
        if [(r.get("event_key"),r.get("revision")) for r in ack] != expected:
            raise RuntimeError("Incomplete cloud acknowledgement; outbox retained")
        if any(r.get("state") not in {"inserted","updated","duplicate","stale"} for r in ack):
            raise RuntimeError("Unknown acknowledgement; outbox retained")
        with self.conn:
            self.conn.executemany("DELETE FROM outbox WHERE id=?", [(row[0],) for row in chosen])
        return len(chosen)

    def close(self):
        self.conn.close()


def inspector_address(path):
    parsed = urlparse(Path(path).read_text().strip())
    if parsed.scheme != "http" or parsed.hostname != "127.0.0.1" or parsed.port != 18181 or parsed.path not in ("", "/") or parsed.query or parsed.username:
        raise ValueError("Unexpected inspector address")
    if len(parsed.fragment) != 48 or any(c not in "0123456789ABCDEFabcdef" for c in parsed.fragment):
        raise ValueError("Invalid inspector key")
    session = hashlib.sha256(parsed.fragment.encode()).hexdigest()
    return "http://127.0.0.1:18181", parsed.fragment, session


def collect(local, base, key, session, outbox):
    headers = {"X-Inspector-Key": key}
    def get(path):
        response = local.get(base+path, headers=headers); response.raise_for_status()
        data = response.json()
        if "error" in data:
            raise RuntimeError("Inspector data unavailable")
        return data
    monitor = get("/api/http?enabled=1")
    for row in monitor.get("requests", [])[:200]:
        service, operation = row.get("service"),row.get("operation")
        if operation not in OPERATIONS.get(service,set()) or service == "Inspector":
            continue
        if row.get("state") not in {"Finished","Error","Aborted","ConnectionTimedOut","TimedOut"}:
            continue
        metadata = {k:row.get(k) for k in ("method","state","status","requestBytes","responseBytes","requestContentType","responseContentType","rpcCode")}
        payload = None
        if row.get("detailsAvailable"):
            identifier = row.get("id")
            if type(identifier) is not int or identifier < 0:
                continue
            decoded = get("/api/http-detail?id="+str(identifier))
            if decoded.get("ready"):
                payload = decoded.get("details")
        topic = "guild" if service == "GuildRpc" and operation == "GetGuild" else None
        outbox.record(session,"rpc:"+str(row["id"]),service,operation,metadata,payload,topic,row.get("firstSeen"))
    for topic,path in (("shipments","/api/shipments"),("inbox","/api/inbox"),("energy","/api/energy")):
        try:
            data = get(path)
        except (httpx.HTTPError, RuntimeError):
            continue
        if topic == "shipments" and not data.get("available"):
            continue
        if topic == "inbox" and not data.get("captured"):
            continue
        # Keep original capture time inside payload; transport polling time is not server freshness.
        data.pop("readAt",None)
        outbox.record(session,"snapshot:"+topic,"Inspector","Snapshot",{},data,topic)


def run(config, once=False):
    cloud = validate_cloud_url(config["cloud_url"])
    if len(config.get("ingest_token", "")) < 32:
        raise ValueError("Configure the source ingest token")
    outbox = Outbox(config["outbox_path"])
    try:
        with httpx.Client(timeout=30, follow_redirects=False, trust_env=False) as client:
            while True:
                # Only ingestion is retried. This bridge has no game mutation route.
                try:
                    outbox.flush(client,cloud,config["ingest_token"])
                except (httpx.HTTPError, RuntimeError):
                    print("Cloud upload unavailable; queued observations retained", flush=True)
                try:
                    base,key,session=inspector_address(config["inspector_url_file"])
                    collect(client,base,key,session,outbox)
                except (OSError, httpx.HTTPError, RuntimeError, ValueError):
                    print("Inspector unavailable; stored cloud snapshots remain historical", flush=True)
                try:
                    sent=outbox.flush(client,cloud,config["ingest_token"])
                    if sent:
                        print(f"Acknowledged {sent} observations",flush=True)
                except (httpx.HTTPError, RuntimeError):
                    print("Cloud upload unavailable; queued observations retained",flush=True)
                if once:
                    break
                time.sleep(max(2, min(int(config.get("interval_seconds",5)),60)))
    finally:
        outbox.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config",required=True); parser.add_argument("--once",action="store_true")
    args=parser.parse_args(); path=Path(args.config)
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise SystemExit("Bridge config contains a credential: chmod 600 the config file")
    run(json.loads(path.read_text()),args.once)


if __name__ == "__main__":
    main()
