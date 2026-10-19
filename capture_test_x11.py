
#!/usr/bin/env python3
"""Capture the SWGOH X11/XWayland window for OpenCV."""
import re
import subprocess
import sys
from pathlib import Path

import cv2

TITLE = "Star Wars: Galaxy of Heroes"
EXPECTED_SIZE = (1920, 1080)
OUTPUT = Path(__file__).resolve().parent / "swgoh.png"


def run(args):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=10
    )


def find_window():
    p = run(["wmctrl", "-l", "-G"])
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip() or "wmctrl failed")

    matches = []
    for line in p.stdout.splitlines():
        # Format: ID desktop x y width height host title
        parts = line.split(None, 7)
        if len(parts) == 8 and parts[7].strip() == TITLE:
            matches.append(parts)

    if len(matches) != 1:
        raise RuntimeError(
            f"Expected exactly one '{TITLE}' window; found {len(matches)}"
        )

    parts = matches[0]
    window_id = parts[0]
    width, height = int(parts[4]), int(parts[5])
    return window_id, width, height


def main():
    try:
        window_id, width, height = find_window()

        if (width, height) != EXPECTED_SIZE:
            raise RuntimeError(
                f"Unexpected window size: {width}x{height}"
            )

        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        xwd_file = OUTPUT.with_suffix(".xwd")

        p = run([
            "xwd", "-silent", "-id", window_id,
            "-out", str(xwd_file)
        ])
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "xwd failed")

        p = run([
            "magick", str(xwd_file), str(OUTPUT)
        ])
        if p.returncode != 0:
            raise RuntimeError(p.stderr.strip() or "ImageMagick failed")

        img = cv2.imread(str(OUTPUT))
        if img is None:
            raise RuntimeError("OpenCV cannot read captured PNG")

        h, w = img.shape[:2]
        if (w, h) != EXPECTED_SIZE:
            raise RuntimeError(f"Invalid captured size: {w}x{h}")

        print(f"Window: {TITLE} [{window_id}]")
        print(f"Capture OK: {OUTPUT} ({w}x{h})")

    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        sys.exit(f"Capture error: {e}")


if __name__ == "__main__":
    main()
