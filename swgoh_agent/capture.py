"""Capture the game window through X11/XWayland (wmctrl + xwd + ImageMagick)."""
from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from .config import Settings


class Window(NamedTuple):
    id: str
    x: int
    y: int
    width: int
    height: int


class Frame(NamedTuple):
    image: np.ndarray
    origin: tuple[int, int]
    captured_at: float
    window: Window


class WindowCapture:
    def __init__(self, title: str, expected_size: tuple[int, int],
                 output: Path, timeout: int):
        self.title = title
        self.expected_size = expected_size
        self.output = output
        self.timeout = timeout

    @classmethod
    def from_settings(cls, s: Settings) -> "WindowCapture":
        return cls(
            s.capture.window_title,
            s.resolution,
            s.capture.output,
            s.capture.timeout_seconds,
        )

    def _run(self, cmd: list[str], tool: str) -> subprocess.CompletedProcess:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=self.timeout
        )
        if p.returncode != 0:
            raise RuntimeError(f"{tool} failed: {p.stderr.strip() or p.stdout.strip()}")
        return p

    def find_window(self) -> Window:
        p = self._run(["wmctrl", "-l", "-G"], "wmctrl")
        matches = []
        for line in p.stdout.splitlines():
            # Format: ID desktop x y width height host title
            parts = line.split(None, 7)
            if len(parts) == 8 and parts[7].strip() == self.title:
                matches.append(parts)
        if len(matches) != 1:
            raise RuntimeError(
                f"Expected exactly one '{self.title}' window; found {len(matches)}"
            )
        m = matches[0]
        return Window(m[0], int(m[2]), int(m[3]), int(m[4]), int(m[5]))

    def assert_click_target(self, frame: Frame) -> None:
        if self.find_window() != frame.window:
            raise RuntimeError("Game window moved, resized or was replaced; click cancelled.")
        active = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], "xprop")
        try:
            active_id = int(active.stdout.rsplit(" ", 1)[-1].strip(), 16)
        except ValueError as exc:
            raise RuntimeError("Cannot verify active window; click cancelled.") from exc
        if active_id != int(frame.window.id, 16):
            raise RuntimeError("Game window is not active; click cancelled.")

    def grab(self) -> Frame:
        window = self.find_window()
        captured_at = time.monotonic()
        ew, eh = self.expected_size
        if (window.width, window.height) != (ew, eh):
            raise RuntimeError(
                f"Game window is {window.width}x{window.height}; expected {ew}x{eh}"
            )

        self.output.parent.mkdir(parents=True, exist_ok=True)
        xwd_path = self.output.with_suffix(".xwd")
        try:
            self._run(
                ["xwd", "-silent", "-id", window.id, "-out", str(xwd_path)],
                "xwd",
            )
            self._run(["magick", str(xwd_path), str(self.output)], "ImageMagick")
        finally:
            xwd_path.unlink(missing_ok=True)

        image = cv2.imread(str(self.output))
        if image is None:
            raise RuntimeError("Screenshot missing or unreadable.")
        h, w = image.shape[:2]
        if (w, h) != (ew, eh):
            raise RuntimeError(f"Captured image is {w}x{h}; expected {ew}x{eh}")
        if self.find_window() != window:
            raise RuntimeError("Game window changed during capture.")
        return Frame(image, (window.x, window.y), captured_at, window)
