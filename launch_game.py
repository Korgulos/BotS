#!/usr/bin/env python3
"""Launch SWGOH through Steam. Configure appid or launch_command in config.json."""
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
steam = CONFIG.get("steam", {})
command = steam.get("launch_command", "").strip()
appid = str(steam.get("appid", "")).strip()

if command:
    argv = command.split()
elif appid:
    argv = ["steam", "-applaunch", appid]
else:
    print("Nije podešen Steam AppID/launch_command u config.json.")
    print("U Steam biblioteci otvori SWGOH > Properties i proveri detalje/prečicu.")
    print("Zatim upiši odgovarajući AppID u config.json. Ne nagađaj AppID.")
    sys.exit(2)

print("Pokrećem:", argv)
subprocess.Popen(argv, start_new_session=True)
wait = int(steam.get("wait_after_launch_seconds", 45))
print(f"Čekam {wait} s da se igra učita...")
time.sleep(wait)
print("Pokretanje je poslato. Proveri da li je igra zaista otvorena.")
