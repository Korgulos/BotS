-- Generated from swgoh_cloud.db; PostgreSQL schema version 1.

CREATE TABLE accounts (
	id VARCHAR(36) NOT NULL,
	alias VARCHAR(80) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (alias)
)

;

CREATE TABLE schema_version (
	version SERIAL NOT NULL,
	PRIMARY KEY (version)
)

;

CREATE TABLE sources (
	id VARCHAR(36) NOT NULL,
	account_id VARCHAR(36) NOT NULL,
	name VARCHAR(80) NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	UNIQUE (account_id, name),
	FOREIGN KEY(account_id) REFERENCES accounts (id)
)

;

CREATE TABLE api_keys (
	id VARCHAR(36) NOT NULL,
	account_id VARCHAR(36) NOT NULL,
	source_id VARCHAR(36),
	role VARCHAR(12) NOT NULL,
	digest VARCHAR(64) NOT NULL,
	enabled BOOLEAN NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE NOT NULL,
	PRIMARY KEY (id),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	UNIQUE (digest)
)

;

CREATE TABLE rpc_events (
	id VARCHAR(36) NOT NULL,
	account_id VARCHAR(36) NOT NULL,
	source_id VARCHAR(36) NOT NULL,
	session VARCHAR(64) NOT NULL,
	event_key VARCHAR(120) NOT NULL,
	revision INTEGER NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	received_at TIMESTAMP WITH TIME ZONE NOT NULL,
	service VARCHAR(64) NOT NULL,
	operation VARCHAR(80) NOT NULL,
	topic VARCHAR(32),
	content_hash VARCHAR(64) NOT NULL,
	metadata JSONB NOT NULL,
	payload JSONB,
	PRIMARY KEY (id),
	UNIQUE (source_id, session, event_key),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	FOREIGN KEY(source_id) REFERENCES sources (id)
)

;
CREATE INDEX events_account_time ON rpc_events (account_id, observed_at, id);

CREATE TABLE battles (
	event_id VARCHAR(36) NOT NULL,
	phase VARCHAR(12),
	battle_type VARCHAR(80),
	seed VARCHAR(32),
	outcome VARCHAR(80),
	record_count INTEGER,
	records_shown INTEGER,
	truncated BOOLEAN NOT NULL,
	PRIMARY KEY (event_id),
	FOREIGN KEY(event_id) REFERENCES rpc_events (id)
)

;

CREATE TABLE guild_members (
	event_id VARCHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	name VARCHAR(120),
	role VARCHAR(80),
	level INTEGER,
	galactic_power VARCHAR(32),
	data JSONB NOT NULL,
	PRIMARY KEY (event_id, ordinal),
	FOREIGN KEY(event_id) REFERENCES rpc_events (id)
)

;

CREATE TABLE inbox_messages (
	event_id VARCHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	type VARCHAR(100),
	subject_key VARCHAR(160),
	starts VARCHAR(32),
	expires VARCHAR(32),
	attachments JSONB NOT NULL,
	PRIMARY KEY (event_id, ordinal),
	FOREIGN KEY(event_id) REFERENCES rpc_events (id)
)

;

CREATE TABLE shipment_offers (
	event_id VARCHAR(36) NOT NULL,
	ordinal INTEGER NOT NULL,
	shop VARCHAR(80),
	offer_id VARCHAR(160),
	definition VARCHAR(160),
	purchased BOOLEAN,
	price JSONB,
	reward JSONB,
	refresh VARCHAR(32),
	PRIMARY KEY (event_id, ordinal),
	FOREIGN KEY(event_id) REFERENCES rpc_events (id)
)

;

CREATE TABLE snapshots (
	account_id VARCHAR(36) NOT NULL,
	source_id VARCHAR(36) NOT NULL,
	topic VARCHAR(32) NOT NULL,
	event_id VARCHAR(36) NOT NULL,
	observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
	payload JSONB NOT NULL,
	PRIMARY KEY (account_id, source_id, topic),
	FOREIGN KEY(account_id) REFERENCES accounts (id),
	FOREIGN KEY(source_id) REFERENCES sources (id),
	FOREIGN KEY(event_id) REFERENCES rpc_events (id)
)

;
INSERT INTO schema_version(version) VALUES (1) ON CONFLICT DO NOTHING;
