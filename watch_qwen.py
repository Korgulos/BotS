#!/usr/bin/env python3
"""Stream a model's thinking/answer live. Defaults come from config.json."""
import argparse
import json
import time

import requests

from swgoh_agent.config import Settings

parser = argparse.ArgumentParser()
parser.add_argument("--model", help="Override the model from config.json")
args = parser.parse_args()

cfg = Settings.load().ollama
URL = cfg.url or "http://127.0.0.1:11434/api/chat"
MODEL = args.model or cfg.model

payload = {
    "model": MODEL,
    "messages": [{
        "role": "user",
        "content": (
            "Analiziraj kratko stanje SWGOH: "
            "main_menu, confidence 0.94. "
            "Vrati JSON sa summary i safe_next_step."
        ),
    }],
    "stream": True,
    "think": True,
    "format": "json",
    "options": {"temperature": 0, "num_predict": 120},
}

print(f"Model: {MODEL}")
print("Čekam odgovor; delovi će se prikazivati uživo...\n")

start = time.monotonic()
STAT_KEYS = ("load_duration", "prompt_eval_count", "eval_count",
             "eval_duration", "done_reason")

try:
    with requests.post(URL, json=payload, stream=True, timeout=(10, 300)) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line:
                continue
            event = json.loads(line.decode("utf-8"))
            message = event.get("message", {})
            for key in ("thinking", "content"):
                if message.get(key):
                    print(message[key], end="", flush=True)
            if event.get("done"):
                print("\n\nZavršeno.")
                print("Ukupno vreme:", round(time.monotonic() - start, 2), "sekundi")
                print("Statistika:", {k: event.get(k) for k in STAT_KEYS})
                break
except requests.RequestException as exc:
    print(f"\nHTTP/API greška: {exc}")
except KeyboardInterrupt:
    print("\nPrekinuto korisničkom komandom.")
