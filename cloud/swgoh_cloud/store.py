import json
from sqlalchemy import select, delete
from sqlalchemy.dialects.postgresql import insert
from . import db


def content_hash(event):
    return db.digest(json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":"), allow_nan=False))


def _object(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def _text(value, limit):
    return str(value)[:limit] if value is not None else None


def _integer(value):
    return value if type(value) is int and -(2**31) <= value < 2**31 else None


def project(conn, event_id, event):
    payload = event.payload
    if payload is None:
        return
    # Revisions replace the normalized projection atomically with the raw event.
    for table in (db.offers, db.inbox, db.members, db.battles):
        conn.execute(delete(table).where(table.c.event_id == event_id))
    topic = event.topic
    if topic == "territory":
        from .territory import checkpoint
        checkpoint(conn, event)
    if topic and topic != "territory":
        query = insert(db.snapshots).values(account_id=event.account_id, source_id=event.source_id,
            topic=topic, event_id=event_id, observed_at=event.observed_at, payload=payload)
        conn.execute(query.on_conflict_do_update(index_elements=["account_id", "source_id", "topic"],
            set_={"event_id": event_id, "observed_at": event.observed_at, "payload": payload},
            where=db.snapshots.c.observed_at <= event.observed_at))
    if topic == "shipments":
        data = _object(payload.get("offers", payload))
        rows = []
        for shop in _rows(data.get("shipments"))[:32]:
            shop = _object(shop)
            for item in _rows(shop.get("items"))[:500]:
                item = _object(item)
                rows.append(dict(event_id=event_id, ordinal=len(rows), shop=_text(shop.get("type"),80),
                    offer_id=_text(item.get("itemId"),160), definition=_text(_object(item.get("reward")).get("definition"),160),
                    purchased=item.get("purchased") if type(item.get("purchased")) is bool else None,
                    price=item.get("cost"), reward=item.get("reward"), refresh=_text(shop.get("nextRefresh"),32)))
        if rows:
            conn.execute(db.offers.insert(), rows)
    if topic == "inbox":
        data = _object(payload.get("inbox", payload))
        rows = [dict(event_id=event_id, ordinal=i, type=_text(m.get("type"),100),
            subject_key=_text(m.get("subjectKey"),160), starts=_text(m.get("starts"),32),
            expires=_text(m.get("expires"),32), attachments=_rows(m.get("attachments")))
            for i,m in enumerate(_rows(data.get("messages"))[:100]) if isinstance(m,dict)]
        if rows:
            conn.execute(db.inbox.insert(), rows)
    if topic == "guild":
        rows = [dict(event_id=event_id, ordinal=i, name=_text(m.get("name"),120),
            role=_text(m.get("role"),80), level=_integer(m.get("level")),
            galactic_power=_text(m.get("galacticPower"),32), data=m)
            for i,m in enumerate(_rows(payload.get("members"))[:100]) if isinstance(m,dict)]
        if rows:
            conn.execute(db.members.insert(), rows)
    if payload.get("kind") == "battle":
        request = _object(payload.get("request")); response = _object(payload.get("response"))
        state = _object(request.get("state"))
        data = _object(response.get("battleData")) if payload.get("phase") == "start" else _object(state.get("data"))
        conn.execute(db.battles.insert().values(event_id=event_id, phase=_text(payload.get("phase"),12),
            battle_type=_text(request.get("type"),80), seed=_text(data.get("battleSeed"),32),
            outcome=_text(request.get("outcome"),80), record_count=_integer(request.get("recordCount")),
            records_shown=len(_rows(request.get("records"))), truncated=bool(payload.get("truncated") or request.get("recordsTruncated"))))


class ProjectionEvent:
    """Identity comes only from the authenticated source, never from a request body."""
    def __init__(self, event, identity):
        self.__dict__.update(event.__dict__)
        self.account_id = identity["account_id"]
        self.source_id = identity["source_id"]


def ingest(conn, identity, batch):
    results = []
    for event in batch.events:
        values = dict(id=db.uid(), account_id=identity["account_id"], source_id=identity["source_id"],
            session=event.session, event_key=event.event_key, revision=event.revision,
            observed_at=event.observed_at, received_at=db.now(), service=event.service, operation=event.operation,
            topic=event.topic, content_hash=content_hash(event), metadata=event.metadata, payload=event.payload)
        inserted = conn.execute(insert(db.events).values(**values).on_conflict_do_nothing(
            index_elements=["source_id", "session", "event_key"]).returning(db.events.c.id)).scalar_one_or_none()
        if inserted:
            event_id = inserted; state = "inserted"
        else:
            row = conn.execute(select(db.events).where(db.events.c.source_id == identity["source_id"],
                db.events.c.session == event.session, db.events.c.event_key == event.event_key).with_for_update()).mappings().one()
            event_id = row["id"]
            if event.revision < row["revision"]:
                results.append(dict(event_key=event.event_key, revision=event.revision, state="stale")); continue
            if event.revision == row["revision"]:
                if row["content_hash"] != values["content_hash"]:
                    raise ValueError("An event revision cannot contain different content")
                results.append(dict(event_key=event.event_key, revision=event.revision, state="duplicate")); continue
            # A device cannot reclassify an existing event under a new RPC/topic.
            if (row["service"], row["operation"], row["topic"]) != (event.service,event.operation,event.topic):
                raise ValueError("An event cannot change its operation or topic")
            conn.execute(db.events.update().where(db.events.c.id == event_id).values(
                revision=event.revision, observed_at=event.observed_at, payload=event.payload,
                metadata=event.metadata, content_hash=values["content_hash"]))
            state = "updated"
        project(conn, event_id, ProjectionEvent(event, identity))
        results.append(dict(event_key=event.event_key, revision=event.revision, state=state))
    return results
