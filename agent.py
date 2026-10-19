
#!/usr/bin/env python3
"""SQLite-backed SWGOH screen agent with safe defaults."""

import base64
import json
import operator
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from string import Template

import cv2
import requests

ROOT = Path(__file__).resolve().parent
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATABASE = ROOT / "agent.sqlite3"

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

SCREENSHOT = ROOT / CFG["capture"]["output"]
LOGFILE = LOG_DIR / "agent.jsonl"
LEGACY_STATEFILE = LOG_DIR / "last_state.json"

last_click = 0.0
actions_done = 0

OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}


def open_database():
    connection = sqlite3.connect(DATABASE, timeout=5)
    connection.row_factory = sqlite3.Row
    return connection


@contextmanager
def database_connection():
    connection = open_database()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def import_legacy_definitions(connection, config):
    """Import definitions from older config.json files on first database creation."""
    if "states" in config:
        states = config["states"]
        if not isinstance(states, dict):
            raise ValueError("'states' must be a JSON object.")
        connection.execute("DELETE FROM state_definitions")
        for position, (name, definition) in enumerate(states.items()):
            if not isinstance(definition, dict):
                definition = {"template": definition}
            connection.execute(
                """
                INSERT INTO state_definitions
                    (name, template, threshold, enabled, sort_order)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    name,
                    definition.get("template") or "",
                    definition.get("threshold"),
                    int(definition.get("enabled", True)),
                    position,
                ),
            )

    if "actions" in config:
        actions = config["actions"]
        if isinstance(actions, dict):
            actions = [
                {"name": name, **definition}
                for name, definition in actions.items()
            ]
        if not isinstance(actions, list):
            raise ValueError("'actions' must be a JSON object or array.")
        connection.execute("DELETE FROM actions")
        for position, action in enumerate(actions):
            save_action(connection, action, position)

    if "observations" in config:
        observations = config["observations"]
        if isinstance(observations, dict):
            observations = [
                {
                    "name": name,
                    **(
                        value
                        if isinstance(value, dict)
                        else {"description": str(value)}
                    ),
                }
                for name, value in observations.items()
            ]
        if not isinstance(observations, list):
            raise ValueError(
                "'observations' must be a JSON object or array."
            )
        connection.execute("DELETE FROM observation_fields")
        for position, observation in enumerate(observations):
            if not isinstance(observation, dict) or not observation.get("name"):
                raise ValueError("Each observation must have a name.")
            connection.execute(
                """
                INSERT INTO observation_fields
                    (name, description, value_type, sort_order)
                VALUES (?, ?, ?, ?)
                """,
                (
                    observation["name"],
                    observation.get("description", ""),
                    observation.get("type", "string or number"),
                    position,
                ),
            )


def initialize_database():
    is_new_database = not DATABASE.exists()
    with database_connection() as connection:
        schema = (ROOT / "agent_schema.sql").read_text(encoding="utf-8")
        connection.executescript(schema)
        if is_new_database:
            seed_data = (ROOT / "agent_seed.sql").read_text(encoding="utf-8")
            connection.executescript(seed_data)
            import_legacy_definitions(connection, CFG)
        import_legacy_state(connection)


def import_legacy_state(connection):
    """Import the former JSON snapshot into SQLite once, if it exists."""
    imported = connection.execute(
        "SELECT value FROM app_metadata WHERE key = 'legacy_state_imported'"
    ).fetchone()
    if imported is not None:
        return

    if LEGACY_STATEFILE.exists():
        state = json.loads(LEGACY_STATEFILE.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("Legacy last_state.json must contain a JSON object.")
        observed_at = datetime.fromtimestamp(
            LEGACY_STATEFILE.stat().st_mtime, timezone.utc
        ).isoformat()
        connection.execute(
            """
            INSERT INTO latest_state (id, payload_json, observed_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO NOTHING
            """,
            (json.dumps(state, ensure_ascii=False), observed_at),
        )

    connection.execute(
        "INSERT INTO app_metadata (key, value) VALUES ('legacy_state_imported', '1')"
    )


def save_action(connection, action, position):
    if not isinstance(action, dict) or not action.get("name"):
        raise ValueError("Each action must have a name.")
    when_state = action.get("when_state")
    if isinstance(when_state, str):
        when_state = [when_state]
    elif not isinstance(when_state, list):
        when_state = []
    conditions = action.get("conditions", [])
    connection.execute(
        """
        INSERT INTO actions (
            name, when_state_json, template, description, enabled,
            requires_manual_review, postcondition_state, cooldown_seconds,
            min_confidence, conditions_json, sort_order
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            json.dumps(conditions, ensure_ascii=False),
            position,
        ),
    )


def log(event):
    event["time"] = datetime.now(timezone.utc).isoformat()
    with LOGFILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def run_command(cmd, timeout):
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def capture():
    """Capture the configured game window through X11/XWayland."""
    timeout = int(CFG["capture"]["timeout_seconds"])
    expected = tuple(CFG["resolution"])
    title = CFG["capture"].get(
        "window_title", "Star Wars: Galaxy of Heroes"
    )

    p = run_command(["wmctrl", "-l", "-G"], timeout)
    if p.returncode != 0:
        raise RuntimeError(
            "wmctrl failed: " + (p.stderr.strip() or p.stdout.strip())
        )

    matches = []
    for line in p.stdout.splitlines():
        parts = line.split(None, 7)
        if len(parts) == 8 and parts[7].strip() == title:
            matches.append(parts)

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one game window; found {len(matches)}"
        )

    window_id = matches[0][0]
    width, height = int(matches[0][4]), int(matches[0][5])

    if (width, height) != expected:
        raise RuntimeError(
            f"Game window is {width}x{height}; "
            f"expected {expected[0]}x{expected[1]}"
        )

    SCREENSHOT.parent.mkdir(parents=True, exist_ok=True)
    xwd_path = SCREENSHOT.with_suffix(".xwd")

    p = run_command(
        ["xwd", "-silent", "-id", window_id, "-out", str(xwd_path)],
        timeout,
    )
    if p.returncode != 0:
        raise RuntimeError(
            "xwd failed: " + (p.stderr.strip() or p.stdout.strip())
        )

    p = run_command(
        ["magick", str(xwd_path), str(SCREENSHOT)],
        timeout,
    )
    if p.returncode != 0:
        raise RuntimeError(
            "ImageMagick failed: "
            + (p.stderr.strip() or p.stdout.strip())
        )

    img = cv2.imread(str(SCREENSHOT))
    if img is None:
        raise RuntimeError("Screenshot missing or unreadable.")

    h, w = img.shape[:2]
    if (w, h) != expected:
        raise RuntimeError(
            f"Captured image is {w}x{h}; "
            f"expected {expected[0]}x{expected[1]}"
        )

    return img


def template_path(name_or_path):
    """Resolve a template key or a relative/absolute template path."""
    templates = CFG.get("templates", {})

    if isinstance(name_or_path, str) and name_or_path in templates:
        value = templates[name_or_path]
    else:
        value = name_or_path

    if not value:
        return None

    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def template_score(img, name_or_path):
    path = template_path(name_or_path)
    if path is None or not path.exists():
        return 0.0, None

    template = cv2.imread(str(path))
    if template is None:
        return 0.0, None

    ih, iw = img.shape[:2]
    th, tw = template.shape[:2]
    if th > ih or tw > iw:
        return 0.0, None

    result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
    _, score, _, location = cv2.minMaxLoc(result)

    point = (
        int(location[0] + tw / 2),
        int(location[1] + th / 2),
    )
    return float(score), point


def configured_states():
    """Return enabled state definitions from SQLite."""
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT name, template, threshold
            FROM state_definitions
            WHERE enabled = 1
            ORDER BY sort_order, name
            """
        ).fetchall()
    return {
        row["name"]: {
            "template": row["template"],
            **(
                {"threshold": row["threshold"]}
                if row["threshold"] is not None
                else {}
            ),
        }
        for row in rows
    }


def detect_state(img):
    """Detect a state using the state definitions stored in SQLite."""
    scores = {}
    states = configured_states()
    default_threshold = float(
        CFG.get("agent", {}).get("template_threshold", 0.80)
    )

    for state_name, definition in states.items():
        if not isinstance(definition, dict):
            definition = {"template": definition}

        template = definition.get("template")
        if not template:
            scores[state_name] = 0.0
            continue

        score, _ = template_score(img, template)
        scores[state_name] = score

    if not scores:
        return "unknown", scores, 0.0

    for state_name, definition in states.items():
        threshold = float(definition.get("threshold", default_threshold))
        if scores[state_name] >= threshold:
            return state_name, scores, scores[state_name]

    best = max(scores, key=scores.get)
    return "unknown", scores, scores[best]


def configured_observations():
    """Return observation fields in their configured SQLite order."""
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT name, description, value_type
            FROM observation_fields
            ORDER BY sort_order, name
            """
        ).fetchall()
    return [
        {
            "name": row["name"],
            "description": row["description"],
            "type": row["value_type"],
        }
        for row in rows
    ]


def configured_prompt(prompt_key, **values):
    with database_connection() as connection:
        row = connection.execute(
            "SELECT template FROM prompts WHERE prompt_key = ?",
            (prompt_key,),
        ).fetchone()
    if row is None:
        raise ValueError(f"Prompt not found in SQLite: {prompt_key}")
    return Template(row["template"]).substitute(values)


def save_latest_state(result):
    observed_at = datetime.now(timezone.utc).isoformat()
    with database_connection() as connection:
        connection.execute(
            """
            INSERT INTO latest_state (id, payload_json, observed_at)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                payload_json = excluded.payload_json,
                observed_at = excluded.observed_at
            """,
            (json.dumps(result, ensure_ascii=False), observed_at),
        )


def ask_ollama(img, state, scores):
    """Ask Ollama to inspect the screenshot and return structured JSON."""
    settings = CFG.get("ollama", {})
    if not settings.get("enabled", False):
        return None

    observations = configured_observations()
    requested_fields = [
        {
            "name": item["name"],
            "description": item.get("description", ""),
            "type": item.get("type", "string or number"),
        }
        for item in observations
        if isinstance(item, dict) and item.get("name")
    ]

    prompt = configured_prompt(
        "visual_observation",
        state=state,
        template_scores_json=json.dumps(scores),
        requested_fields_json=json.dumps(
            requested_fields, ensure_ascii=False
        ),
    )

    try:
        url = settings["url"].rstrip("/")
        if not url.endswith("/api/chat"):
            raise ValueError(
                f"Expected Ollama /api/chat endpoint, got: {url}"
            )

        payload = {
            "model": settings["model"],
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": "json",
            "think": False,
            "options": {
                "temperature": 0,
                "num_predict": int(settings.get("num_predict", 400)),
            },
        }

        if settings.get("send_image", True):
            ok, encoded = cv2.imencode(".png", img)
            if not ok:
                raise RuntimeError("Could not encode screenshot for Ollama.")
            payload["messages"][0]["images"] = [
                base64.b64encode(encoded.tobytes()).decode("ascii")
            ]

        response = requests.post(
            url,
            json=payload,
            timeout=(5, int(settings.get("timeout_seconds", 180))),
        )
        response.raise_for_status()

        content = response.json().get("message", {}).get("content", "")
        if not content:
            raise ValueError("Ollama returned an empty response.")

        result = json.loads(content)
        if not isinstance(result, dict):
            raise ValueError("Ollama response must be a JSON object.")

        if not isinstance(result.get("values", {}), dict):
            result["values"] = {}

        return result

    except Exception as exc:
        log({"type": "ollama_error", "error": str(exc)})
        return None


def get_field(data, field):
    """Support simple paths such as 'energy' or 'values.energy'."""
    current = data
    for part in field.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def condition_matches(condition, ai):
    """Evaluate one configured condition against the AI observation."""
    field = condition.get("field", "")
    op_name = condition.get("operator", "==")
    expected = condition.get("value")

    if not field or op_name not in OPERATORS:
        return False

    # Bare names are looked up inside values; dotted paths are explicit.
    if "." not in field:
        actual = get_field(ai.get("values", {}), field)
    else:
        actual = get_field(ai, field)

    if actual is None:
        return False

    try:
        return bool(OPERATORS[op_name](actual, expected))
    except (TypeError, ValueError):
        return False


def action_list():
    with database_connection() as connection:
        rows = connection.execute(
            """
            SELECT name, when_state_json, template, description, enabled,
                   requires_manual_review, postcondition_state,
                   cooldown_seconds, min_confidence, conditions_json
            FROM actions
            ORDER BY sort_order, name
            """
        ).fetchall()
    return [
        {
            "name": row["name"],
            "when_state": json.loads(row["when_state_json"]),
            "template": row["template"],
            "description": row["description"],
            "enabled": bool(row["enabled"]),
            "requires_manual_review": bool(row["requires_manual_review"]),
            "postcondition_state": row["postcondition_state"],
            "cooldown_seconds": row["cooldown_seconds"],
            "conditions": json.loads(row["conditions_json"]),
            **(
                {"min_confidence": row["min_confidence"]}
                if row["min_confidence"] is not None
                else {}
            ),
        }
        for row in rows
    ]


def click_xy(x, y):
    global last_click

    input_cfg = CFG["input"]
    if not input_cfg.get("enabled", False):
        raise RuntimeError("Input backend disabled in config.json.")

    now = time.monotonic()
    wait = float(
        input_cfg.get("min_seconds_between_clicks", 2.0)
    ) - (now - last_click)

    if wait > 0:
        time.sleep(wait)

    subprocess.run(
        ["ydotool", "mousemove", "--absolute", str(x), str(y)],
        check=True,
        timeout=5,
    )
    time.sleep(0.25)
    subprocess.run(
        ["ydotool", "click", input_cfg.get("click_button", "0xC0")],
        check=True,
        timeout=5,
    )
    last_click = time.monotonic()


def execute_one_safe_action(img, state, ai):
    """Execute at most one action that passes all configured checks."""
    global actions_done

    settings = CFG["agent"]

    if settings.get("dry_run", True):
        return "dry_run: no click sent"

    if actions_done >= int(settings.get("max_actions_per_session", 20)):
        return "action_limit_reached"

    if state == "unknown":
        return "unknown_state: no click sent"

    if ai is None and settings.get("require_ollama_for_actions", False):
        return "ollama_observation_missing: no click sent"

    for action in action_list():
        if not action.get("enabled", False):
            continue
        if action.get("requires_manual_review", True):
            continue

        expected_state = action.get("when_state")
        if isinstance(expected_state, str):
            expected_states = [expected_state]
        elif isinstance(expected_state, list):
            expected_states = expected_state
        else:
            expected_states = []

        if state not in expected_states:
            continue

        conditions = action.get("conditions", [])
        if conditions:
            if ai is None:
                continue
            if not all(condition_matches(c, ai) for c in conditions):
                continue

        template = action.get("template")
        if not template:
            continue

        score, point = template_score(img, template)
        minimum = float(
            action.get(
                "min_confidence",
                settings.get("min_confidence_for_action", 0.94),
            )
        )

        if point is None or score < minimum:
            continue

        x, y = point
        h, w = img.shape[:2]
        if not (20 <= x < w - 20 and 20 <= y < h - 20):
            continue

        name = action.get("name", "unnamed_action")
        print(f"Validated action {name}: score={score:.3f} at {point}")

        click_xy(x, y)
        actions_done += 1

        log({
            "type": "action",
            "name": name,
            "state": state,
            "score": score,
            "point": point,
            "conditions": conditions,
        })

        time.sleep(float(action.get("cooldown_seconds", 5)))
        return "clicked:" + name

    return "no eligible action"


def main():
    global actions_done

    initialize_database()
    settings = CFG["agent"]
    print("SWGOH Agent — SQLite-backed mode")
    print("Dry-run:", settings.get("dry_run", True))
    print("Input enabled:", CFG["input"].get("enabled", False))
    print("Configured states:", list(configured_states()))
    print("Configured actions:", len(action_list()))
    print("Ctrl+C stops the agent. Unknown state = no click.")

    errors = 0

    while True:
        started = time.monotonic()

        try:
            img = capture()
            state, scores, confidence = detect_state(img)
            ai = ask_ollama(img, state, scores)

            result = {
                "state": state,
                "confidence": confidence,
                "scores": scores,
                "ai": ai,
                "actions_done": actions_done,
            }

            save_latest_state(result)
            log({"type": "observation", **result})

            print(
                f"State={state} confidence={confidence:.3f} "
                f"scores={scores}"
            )
            if ai:
                print("AI:", json.dumps(ai, ensure_ascii=False))

            outcome = execute_one_safe_action(img, state, ai)
            print("Action:", outcome)
            errors = 0

        except KeyboardInterrupt:
            print("\nStopped by user.")
            break

        except Exception as exc:
            errors += 1
            log({
                "type": "error",
                "error": str(exc),
                "count": errors,
            })
            print(
                f"Error {errors}/"
                f"{settings['max_consecutive_errors']}: {exc}"
            )

            if errors >= int(settings["max_consecutive_errors"]):
                print("Too many errors; stopping safely.")
                break

        elapsed = time.monotonic() - started
        time.sleep(
            max(0, float(settings["loop_interval_seconds"]) - elapsed)
        )


if __name__ == "__main__":
    main()
