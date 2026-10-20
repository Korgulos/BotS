"""Template matching and screen-state detection."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, NamedTuple, Sequence

import cv2
import numpy as np

from .store import StateDef


class Match(NamedTuple):
    score: float
    point: tuple[int, int] | None  # centre of the best match


NO_MATCH = Match(0.0, None)


class Detection(NamedTuple):
    state: str
    scores: dict[str, float]
    confidence: float


class TemplateMatcher:
    """Matches templates against a frame; template images are cached by mtime."""

    def __init__(self, resolver: Callable[[str | None], Path | None]):
        self._resolve = resolver
        self._cache: dict[Path, tuple[int, np.ndarray]] = {}

    def _load(self, path: Path) -> np.ndarray | None:
        try:
            mtime = path.stat().st_mtime_ns
        except OSError:
            self._cache.pop(path, None)
            return None
        cached = self._cache.get(path)
        if cached and cached[0] == mtime:
            return cached[1]
        image = cv2.imread(str(path))
        if image is None:
            self._cache.pop(path, None)
            return None
        self._cache[path] = (mtime, image)
        return image

    def available(self, ref: str | None) -> bool:
        """Check a reference can be resolved and decoded before starting."""
        path = self._resolve(ref)
        return path is not None and self._load(path) is not None

    def match(self, img: np.ndarray, ref: str | None) -> Match:
        path = self._resolve(ref)
        template = self._load(path) if path else None
        if template is None:
            return NO_MATCH

        ih, iw = img.shape[:2]
        th, tw = template.shape[:2]
        if th > ih or tw > iw:
            return NO_MATCH

        result = cv2.matchTemplate(img, template, cv2.TM_CCOEFF_NORMED)
        _, score, _, loc = cv2.minMaxLoc(result)
        return Match(float(score), (int(loc[0] + tw / 2), int(loc[1] + th / 2)))


def detect_state(img: np.ndarray, states: Sequence[StateDef],
                 matcher: TemplateMatcher, default_threshold: float) -> Detection:
    """Score every state; the first (in sort order) over its threshold wins."""
    if not states:
        return Detection("unknown", {}, 0.0)

    scores = {s.name: matcher.match(img, s.template).score for s in states}

    for s in states:
        threshold = default_threshold if s.threshold is None else float(s.threshold)
        if scores[s.name] >= threshold:
            return Detection(s.name, scores, scores[s.name])

    return Detection("unknown", scores, max(scores.values()))
