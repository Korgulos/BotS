"""Capture the game window through X11/XWayland (wmctrl + xwd + ImageMagick)."""
from __future__ import annotations

import subprocess
import time
import tempfile
from pathlib import Path
from typing import NamedTuple

import cv2
import numpy as np

from .config import Settings


class ClickNotReady(RuntimeError):
    """A pre-click check rejected input; no mouse button was dispatched."""


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
        try:
            if self.find_window() != frame.window:
                raise ClickNotReady("Game window moved, resized or was replaced.")
            active = self._run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], "xprop")
            try:
                active_id = int(active.stdout.rsplit(" ", 1)[-1].strip(), 16)
            except ValueError as exc:
                raise ClickNotReady("Cannot verify active window.") from exc
            if active_id != int(frame.window.id, 16):
                raise ClickNotReady("Game window is not active; focus the game to continue.")
        except ClickNotReady:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise ClickNotReady(f"Cannot verify game window: {exc}") from exc

    def focus_window(self, frame: Frame) -> None:
        """Activate only the captured window, then wait briefly for focus."""
        try:
            if self.find_window() != frame.window:
                raise ClickNotReady("Window changed before automatic focus.")
            try:
                self.assert_click_target(frame)
                return  # Already focused; avoid unnecessary activation requests.
            except ClickNotReady:
                pass
            self._run(["wmctrl", "-i", "-a", frame.window.id], "wmctrl")
            for attempt in range(10):
                try:
                    self.assert_click_target(frame)
                    return
                except ClickNotReady:
                    if attempt == 9:
                        raise
                    time.sleep(0.1)
        except ClickNotReady:
            raise
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            raise ClickNotReady(f"Automatic focus failed: {exc}") from exc

    def grab(self) -> Frame:
        window = self.find_window()
        captured_at = time.monotonic()
        ew, eh = self.expected_size
        if (window.width, window.height) != (ew, eh):
            raise RuntimeError(
                f"Game window is {window.width}x{window.height}; expected {ew}x{eh}"
            )

        self.output.parent.mkdir(parents=True, exist_ok=True)
        # Unique intermediates and atomic publication keep UI readers from
        # observing a partially written PNG and avoid shared .xwd collisions.
        with tempfile.TemporaryDirectory(prefix=".capture-", dir=self.output.parent) as directory:
            xwd_path = Path(directory) / "frame.xwd"
            png_path = Path(directory) / "frame.png"
            self._run(["xwd", "-silent", "-id", window.id, "-out", str(xwd_path)], "xwd")
            self._run(["magick", str(xwd_path), str(png_path)], "ImageMagick")
            image = cv2.imread(str(png_path))
            if image is None:
                raise RuntimeError("Screenshot missing or unreadable.")
            h, w = image.shape[:2]
            if (w, h) != (ew, eh):
                raise RuntimeError(f"Captured image is {w}x{h}; expected {ew}x{eh}")
            if self.find_window() != window:
                raise RuntimeError("Game window changed during capture.")
            png_path.replace(self.output)
        return Frame(image, (window.x, window.y), captured_at, window)
