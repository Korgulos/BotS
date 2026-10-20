"""SQLite persistence: rule definitions, prompts and the latest observation."""
from __future__ import annotations

import json
import sqlite3
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterator

from .config import ROOT
from .action_types import postconditions

DATABASE = ROOT / "agent.sqlite3"
SCHEMA_FILE = ROOT / "agent_schema.sql"
SEED_FILE = ROOT / "agent_seed.sql"
LEGACY_STATE_FILE = ROOT / "logs" / "last_state.json"


@dataclass(frozen=True)
class StateDef:
    name: str
    template: str
    threshold: float | None


@dataclass(frozen=True)
class ObservationField:
    name: str
    description: str
    type: str


@dataclass(frozen=True)
class ActionRule:
    name: str
    when_state: tuple[str, ...]
    template: str | None
    description: str
    enabled: bool
    requires_manual_review: bool
    postcondition_state: str | None
    cooldown_seconds: float
    min_confidence: float | None
    conditions: tuple[dict, ...]
    kind: str = "click"
    parameters: dict = field(default_factory=dict)
    postcondition_states: tuple[str, ...] | None = None

    @property
    def expected_states(self) -> tuple[str, ...]:
        return (self.postcondition_states if self.postcondition_states is not None
                else ((self.postcondition_state,) if self.postcondition_state else ()))


@dataclass(frozen=True)
class Rules:
    """Everything editable in the database, read in one transaction."""
    states: tuple[StateDef, ...]
    actions: tuple[ActionRule, ...]
    observations: tuple[ObservationField, ...]
    prompts: dict[str, str]


class Store:
    def __init__(self, legacy_config: dict[str, Any] | None = None):
        self._legacy_config = legacy_config or {}

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(DATABASE, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    # -- setup ---------------------------------------------------------------

    def initialize(self) -> None:
        with self._connect() as db:
            db.executescript(SCHEMA_FILE.read_text(encoding="utf-8"))
            db.execute("BEGIN IMMEDIATE")
            migrate_actions(db)
            if _needs_seed(db):
                # execute() keeps seed, legacy imports and metadata atomic.
                statement = ""
                for line in SEED_FILE.read_text(encoding="utf-8").splitlines(True):
                    statement += line
                    if sqlite3.complete_statement(statement):
                        db.execute(statement)
                        statement = ""
                if statement.strip():
                    raise ValueError("Incomplete seed SQL statement")
                _import_legacy_definitions(db, self._legacy_config)
            migrate_postconditions(db)
            db.execute(
                "INSERT OR IGNORE INTO app_metadata (key, value) "
                "VALUES ('seeded', '1')"
            )
            _import_legacy_state(db)

    # -- reads / writes ------------------------------------------------------

    def load_rules(self) -> Rules:
        with self._connect() as db:
            states = tuple(
                StateDef(r["name"], r["template"], r["threshold"])
                for r in db.execute(
                    "SELECT name, template, threshold FROM state_definitions "
                    "WHERE enabled = 1 ORDER BY sort_order, name"
                )
            )
            actions = tuple(
                _row_to_action(r)
                for r in db.execute(
                    "SELECT * FROM actions ORDER BY sort_order, name"
                )
            )
            observations = tuple(
                ObservationField(r["name"], r["description"], r["value_type"])
                for r in db.execute(
                    "SELECT name, description, value_type "
                    "FROM observation_fields ORDER BY sort_order, name"
                )
            )
            prompts = {
                r["prompt_key"]: r["template"]
                for r in db.execute("SELECT prompt_key, template FROM prompts")
            }
        return Rules(states, actions, observations, prompts)

    def save_latest_state(self, result: dict) -> None:
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO latest_state (id, payload_json, observed_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    observed_at = excluded.observed_at
                """,
                (
                    json.dumps(result, ensure_ascii=False),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


# -- row mapping -------------------------------------------------------------

def _row_to_action(row: sqlite3.Row) -> ActionRule:
    when_state = json.loads(row["when_state_json"])
    if isinstance(when_state, str):
        when_state = [when_state]
    return ActionRule(
        name=row["name"],
        when_state=tuple(when_state) if isinstance(when_state, list) else (),
        template=row["template"],
        description=row["description"],
        enabled=bool(row["enabled"]),
        requires_manual_review=bool(row["requires_manual_review"]),
        postcondition_state=row["postcondition_state"],
        cooldown_seconds=float(row["cooldown_seconds"]),
        min_confidence=row["min_confidence"],
        conditions=tuple(json.loads(row["conditions_json"])),
        kind=row["kind"],
        parameters=json.loads(row["parameters_json"]),
        postcondition_states=tuple(postconditions(dict(row))),
    )


# -- first-run seeding and legacy imports ------------------------------------

def migrate_actions(db: sqlite3.Connection) -> None:
    """Atomically expand SQLite's kind CHECK while preserving existing records."""
    sql = db.execute("SELECT sql FROM sqlite_master WHERE name='actions'").fetchone()[0]
    columns = [r[1] for r in db.execute('PRAGMA table_info(actions)')]
    if "'click_text'" in sql and 'parameters_json' in columns:
        return
    canonical = re.search(r'CREATE TABLE IF NOT EXISTS actions \(.*?\n\);',
                          SCHEMA_FILE.read_text(), re.S).group(0)
    db.execute(canonical.replace('IF NOT EXISTS actions', 'actions_upgrade'))
    new_columns = {r[1] for r in db.execute('PRAGMA table_info(actions_upgrade)')}
    if set(columns) - new_columns:
        raise ValueError('Action migration found custom columns; refusing to discard them')
    objects = [r[0] for r in db.execute(
        "SELECT sql FROM sqlite_master WHERE tbl_name='actions' AND type IN ('index','trigger') AND sql IS NOT NULL")]
    quoted = ','.join('"'+c+'"' for c in columns)
    db.execute(f'INSERT INTO actions_upgrade ({quoted}) SELECT {quoted} FROM actions')
    db.execute('DROP TABLE actions')
    db.execute('ALTER TABLE actions_upgrade RENAME TO actions')
    for statement in objects:
        db.execute(statement)


def migrate_postconditions(db: sqlite3.Connection) -> None:
    columns = {row[1] for row in db.execute('PRAGMA table_info(actions)')}
    if 'postcondition_states_json' not in columns:
        db.execute('ALTER TABLE actions ADD COLUMN postcondition_states_json TEXT')
    for name, state in db.execute('SELECT name,postcondition_state FROM actions WHERE postcondition_states_json IS NULL').fetchall():
        db.execute('UPDATE actions SET postcondition_states_json=? WHERE name=?',
                   (json.dumps([state] if state else []), name))


def _needs_seed(db: sqlite3.Connection) -> bool:
    """Seed once. Older databases (no flag) are only seeded if they are empty."""
    if db.execute(
        "SELECT 1 FROM app_metadata WHERE key = 'seeded'"
    ).fetchone():
        return False
    return not any(
        db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
        for table in ("state_definitions", "actions", "prompts")
    )


def _named_list(value: Any, label: str, wrap=None) -> list[dict]:
    """Accept {name: definition} or [{name: ..., ...}] legacy layouts."""
    if isinstance(value, dict):
        items = []
        for name, definition in value.items():
            if isinstance(definition, dict):
                items.append({"name": name, **definition})
            elif wrap is not None:
                items.append({"name": name, **wrap(definition)})
            else:
                raise ValueError(f"Invalid definition for '{name}' in '{label}'.")
        return items
    if not isinstance(value, list):
        raise ValueError(f"'{label}' must be a JSON object or array.")
    return value


def _import_legacy_definitions(db: sqlite3.Connection, config: dict) -> None:
    if "states" in config:
        states = config["states"]
        if not isinstance(states, dict):
            raise ValueError("'states' must be a JSON object.")
        db.execute("DELETE FROM state_definitions")
        for position, (name, definition) in enumerate(states.items()):
            if not isinstance(definition, dict):
                definition = {"template": definition}
            db.execute(
                "INSERT INTO state_definitions "
                "(name, template, threshold, enabled, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    name,
                    definition.get("template") or "",
                    definition.get("threshold"),
                    int(definition.get("enabled", True)),
                    position,
                ),
            )

    if "actions" in config:
        db.execute("DELETE FROM actions")
        for position, action in enumerate(
            _named_list(config["actions"], "actions")
        ):
            _insert_action(db, action, position)

    if "observations" in config:
        observations = _named_list(
            config["observations"],
            "observations",
            wrap=lambda v: {"description": str(v)},
        )
        db.execute("DELETE FROM observation_fields")
        for position, obs in enumerate(observations):
            if not isinstance(obs, dict) or not obs.get("name"):
                raise ValueError("Each observation must have a name.")
            db.execute(
                "INSERT INTO observation_fields "
                "(name, description, value_type, sort_order) "
                "VALUES (?, ?, ?, ?)",
                (
                    obs["name"],
                    obs.get("description", ""),
                    obs.get("type", "string or number"),
                    position,
                ),
            )


def _insert_action(db: sqlite3.Connection, action: dict, position: int) -> None:
    if not isinstance(action, dict) or not action.get("name"):
        raise ValueError("Each action must have a name.")
    when_state = action.get("when_state")
    if isinstance(when_state, str):
        when_state = [when_state]
    elif not isinstance(when_state, list):
        when_state = []
    db.execute(
        """
        INSERT INTO actions (
            name, when_state_json, template, description, enabled,
            requires_manual_review, postcondition_state, cooldown_seconds,
            min_confidence, conditions_json, sort_order, kind, parameters_json, postcondition_states_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            action["name"],
            json.dumps(when_state, ensure_ascii=False),
            action.get("template"),
            action.get("description", ""),
            int(action.get("enabled", False)),
            int(action.get("requires_manual_review", True)),
            action.get("postcondition_state"),
            action.get("cooldown_seconds", 5),
            action.get("min_confidence"),
            json.dumps(action.get("conditions", []), ensure_ascii=False),
            position,
            action.get("kind", "click"),
            json.dumps(action.get("parameters", {})),
            json.dumps(postconditions(action)),
        ),
    )


def _import_legacy_state(db: sqlite3.Connection) -> None:
    """Import logs/last_state.json once, if it exists."""
    if db.execute(
        "SELECT 1 FROM app_metadata WHERE key = 'legacy_state_imported'"
    ).fetchone():
        return

    if LEGACY_STATE_FILE.exists():
        state = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Legacy last_state.json must contain a JSON object.")
        observed_at = datetime.fromtimestamp(
            LEGACY_STATE_FILE.stat().st_mtime, timezone.utc
        ).isoformat()
        db.execute(
            "INSERT INTO latest_state (id, payload_json, observed_at) "
            "VALUES (1, ?, ?) ON CONFLICT(id) DO NOTHING",
            (json.dumps(state, ensure_ascii=False), observed_at),
        )

    db.execute(
        "INSERT INTO app_metadata (key, value) "
        "VALUES ('legacy_state_imported', '1')"
    )
