import hashlib
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (MetaData, Table, Column, String, Integer, Boolean, DateTime,
                        ForeignKey, UniqueConstraint, Index, JSON, create_engine, text)
from sqlalchemy.dialects.postgresql import JSONB

meta = MetaData()
json_type = JSON().with_variant(JSONB(), "postgresql")

def uid():
    return str(uuid4())

def now():
    return datetime.now(timezone.utc)

def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()

accounts = Table("accounts", meta,
    Column("id", String(36), primary_key=True), Column("alias", String(80), unique=True, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False))
sources = Table("sources", meta,
    Column("id", String(36), primary_key=True), Column("account_id", ForeignKey("accounts.id"), nullable=False),
    Column("name", String(80), nullable=False), Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("account_id", "name"))
keys = Table("api_keys", meta,
    Column("id", String(36), primary_key=True), Column("account_id", ForeignKey("accounts.id"), nullable=False),
    Column("source_id", ForeignKey("sources.id")), Column("role", String(12), nullable=False),
    Column("digest", String(64), unique=True, nullable=False), Column("enabled", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False))
events = Table("rpc_events", meta,
    Column("id", String(36), primary_key=True), Column("account_id", ForeignKey("accounts.id"), nullable=False),
    Column("source_id", ForeignKey("sources.id"), nullable=False), Column("session", String(64), nullable=False),
    Column("event_key", String(120), nullable=False), Column("revision", Integer, nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False), Column("received_at", DateTime(timezone=True), nullable=False),
    Column("service", String(64), nullable=False), Column("operation", String(80), nullable=False),
    Column("topic", String(32)), Column("content_hash", String(64), nullable=False),
    Column("metadata", json_type, nullable=False), Column("payload", json_type),
    UniqueConstraint("source_id", "session", "event_key"))
Index("events_account_time", events.c.account_id, events.c.observed_at, events.c.id)
snapshots = Table("snapshots", meta,
    Column("account_id", ForeignKey("accounts.id"), primary_key=True),
    Column("source_id", ForeignKey("sources.id"), primary_key=True), Column("topic", String(32), primary_key=True),
    Column("event_id", ForeignKey("rpc_events.id"), nullable=False),
    Column("observed_at", DateTime(timezone=True), nullable=False), Column("payload", json_type, nullable=False))
offers = Table("shipment_offers", meta,
    Column("event_id", ForeignKey("rpc_events.id"), primary_key=True), Column("ordinal", Integer, primary_key=True),
    Column("shop", String(80)), Column("offer_id", String(160)), Column("definition", String(160)),
    Column("purchased", Boolean), Column("price", json_type), Column("reward", json_type), Column("refresh", String(32)))
inbox = Table("inbox_messages", meta,
    Column("event_id", ForeignKey("rpc_events.id"), primary_key=True), Column("ordinal", Integer, primary_key=True),
    Column("type", String(100)), Column("subject_key", String(160)), Column("starts", String(32)), Column("expires", String(32)),
    Column("attachments", json_type, nullable=False))
members = Table("guild_members", meta,
    Column("event_id", ForeignKey("rpc_events.id"), primary_key=True), Column("ordinal", Integer, primary_key=True),
    Column("name", String(120)), Column("role", String(80)), Column("level", Integer),
    Column("galactic_power", String(32)), Column("data", json_type, nullable=False))
battles = Table("battles", meta,
    Column("event_id", ForeignKey("rpc_events.id"), primary_key=True), Column("phase", String(12)),
    Column("battle_type", String(80)), Column("seed", String(32)), Column("outcome", String(80)),
    Column("record_count", Integer), Column("records_shown", Integer), Column("truncated", Boolean, nullable=False))
version = Table("schema_version", meta, Column("version", Integer, primary_key=True))


def engine_for(url):
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    return create_engine(url, pool_pre_ping=True, pool_size=3, max_overflow=2,
                         connect_args={"connect_timeout": 10})


def migrate(engine):
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(743602941)"))
        meta.create_all(conn)
        found = conn.execute(version.select()).scalars().all()
        if found and found != [1]:
            raise RuntimeError("Unsupported database schema version")
        if not found:
            conn.execute(version.insert().values(version=1))
