CREATE TABLE IF NOT EXISTS state_definitions (
    name TEXT PRIMARY KEY,
    template TEXT NOT NULL,
    threshold REAL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS actions (
    name TEXT PRIMARY KEY,
    when_state_json TEXT NOT NULL DEFAULT '[]',
    template TEXT,
    description TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    requires_manual_review INTEGER NOT NULL DEFAULT 1
        CHECK (requires_manual_review IN (0, 1)),
    postcondition_state TEXT,
    cooldown_seconds REAL NOT NULL DEFAULT 5,
    min_confidence REAL,
    conditions_json TEXT NOT NULL DEFAULT '[]',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS observation_fields (
    name TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    value_type TEXT NOT NULL DEFAULT 'string or number',
    sort_order INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS prompts (
    prompt_key TEXT PRIMARY KEY,
    template TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS latest_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
