#!/usr/bin/env python3
"""Capture a screenshot through KDE Spectacle and validate its dimensions."""
import json
import subprocess
from pathlib import Path
import cv2

ROOT = Path(__file__).resolve().parent
cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
out = ROOT / cfg["capture"]["output"]
timeout = int(cfg["capture"]["timeout_seconds"])

cmd = ["spectacle", "--activewindow", "--background", "--nonotify", "--output", str(out)]
print("Running:", cmd)
p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
if p.returncode != 0:
    print("Spectacle error:", p.stderr.strip() or p.stdout.strip())
    raise SystemExit(p.returncode)

img = cv2.imread(str(out))
if img is None:
    raise SystemExit(f"Screenshot was not readable: {out}")
h, w = img.shape[:2]
print(f"Screenshot OK: {out} ({w}x{h})")
if (w, h) != tuple(cfg["resolution"]):
    print("WARNING: screenshot resolution differs from config.json; template coordinates may not match.")
