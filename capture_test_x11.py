#!/usr/bin/env python3
"""Capture the SWGOH X11/XWayland window for OpenCV (shares code with agent)."""
import sys

import subprocess

from swgoh_agent.capture import WindowCapture
from swgoh_agent.config import Settings


def main():
    settings = Settings.load()
    capture = WindowCapture.from_settings(settings)
    try:
        window = capture.find_window()
        frame = capture.grab()
    except (RuntimeError, subprocess.TimeoutExpired, FileNotFoundError) as e:
        sys.exit(f"Capture error: {e}")

    h, w = frame.image.shape[:2]
    print(f"Window: {settings.capture.window_title} [{window.id}] "
          f"at {window.x},{window.y}")
    print(f"Capture OK: {capture.output} ({w}x{h})")


if __name__ == "__main__":
    main()
