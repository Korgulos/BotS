"""Append-only JSONL event log."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


class EventLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, event_type: str, **fields) -> None:
        record = {
            "type": event_type,
            **fields,
            "time": datetime.now(timezone.utc).isoformat(),
        }
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
