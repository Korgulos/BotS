#!/usr/bin/env python3
"""Create a template from last_screen.png using x,y,width,height."""
import argparse
import cv2
from pathlib import Path

ROOT = Path(__file__).resolve().parent
p = argparse.ArgumentParser()
p.add_argument("name", help="Template name, e.g. battle_button")
p.add_argument("x", type=int)
p.add_argument("y", type=int)
p.add_argument("width", type=int)
p.add_argument("height", type=int)
p.add_argument("--source", default="last_screen.png")
args = p.parse_args()

src = ROOT / args.source
img = cv2.imread(str(src))
if img is None:
    raise SystemExit(f"Cannot read {src}; run capture_test.py first.")
h, w = img.shape[:2]
x, y, cw, ch = args.x, args.y, args.width, args.height
if x < 0 or y < 0 or cw <= 0 or ch <= 0 or x+cw > w or y+ch > h:
    raise SystemExit(f"Crop outside image ({w}x{h}).")
crop = img[y:y+ch, x:x+cw]
out = ROOT / "templates" / f"{args.name}.png"
out.parent.mkdir(parents=True, exist_ok=True)
if not cv2.imwrite(str(out), crop):
    raise SystemExit("Failed to save template.")
print(f"Saved {out} ({cw}x{ch})")
