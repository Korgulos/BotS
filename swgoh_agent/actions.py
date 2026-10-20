"""Safety-gated action execution."""
from __future__ import annotations

import operator
import subprocess
import time
from typing import Any, Callable, Sequence

import numpy as np

from .config import AgentSettings, InputSettings
from .capture import Frame
from .store import ActionRule
from .vision import TemplateMatcher

OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}

EDGE_MARGIN = 20  # never click within this many px of the screenshot edge


def lookup(data: Any, path: str) -> Any:
    """Resolve 'a.b.c' inside nested dicts; None if any step is missing."""
    for part in path.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def condition_matches(condition: dict, ai: dict) -> bool:
    field = condition.get("field", "")
    op = OPERATORS.get(condition.get("operator", "=="))
    if not field or op is None:
        return False

    # Bare names are looked up inside "values"; dotted paths are explicit.
    actual = lookup(ai if "." in field else ai.get("values", {}), field)
    if actual is None:
        return False

    try:
        return bool(op(actual, condition.get("value")))
    except (TypeError, ValueError):
        return False


class Clicker:
    """ydotool-backed mouse input with a minimum delay between clicks."""

    def __init__(self, cfg: InputSettings):
        self.cfg = cfg
        self._last_click = 0.0

    def click(self, x: int, y: int, before_click: Callable[[], None]) -> None:
        if not self.cfg.enabled:
            raise RuntimeError("Input backend disabled in config.json.")

        wait = self.cfg.min_seconds_between_clicks - (
            time.monotonic() - self._last_click
        )
        if wait > 0:
            time.sleep(wait)

        before_click()
        subprocess.run(
            ["ydotool", "mousemove", "--absolute", str(x), str(y)],
            check=True, timeout=5,
        )
        time.sleep(0.25)
        before_click()
        subprocess.run(
            ["ydotool", "click", self.cfg.click_button],
            check=True, timeout=5,
        )
        self._last_click = time.monotonic()


class ActionExecutor:
    """Executes at most one action per call, and only if every check passes."""

    def __init__(self, cfg: AgentSettings, matcher: TemplateMatcher,
                 clicker: Clicker, log: Callable[..., None]):
        self.cfg = cfg
        self.matcher = matcher
        self.clicker = clicker
        self.log = log
        self.actions_done = 0
        self.pending: tuple[ActionRule, float, float] | None = None
        self.halted = False

    def check_pending(self, state: str) -> str | None:
        if self.halted:
            return "halted: restart after manual inspection"
        if self.pending is None:
            return None
        action, ready_at, deadline = self.pending
        now = time.monotonic()
        if now < ready_at:
            return f"cooldown:{action.name}"
        if state == action.postcondition_state:
            self.pending = None
            self.log("postcondition_confirmed", name=action.name, state=state)
            return f"confirmed:{action.name}"
        if now >= deadline:
            self.halted = True
            self.log("postcondition_failed", name=action.name,
                     expected=action.postcondition_state, state=state)
            return f"postcondition_failed:{action.name}; halted"
        return f"awaiting_postcondition:{action.name}"

    def run(self, frame: Frame, state: str, ai: dict | None,
            actions: Sequence[ActionRule], *,
            refresh: Callable[[], tuple[Frame, str]],
            guard: Callable[[Frame], None]) -> str:
        cfg = self.cfg
        if cfg.dry_run:
            return "dry_run: no click sent"
        pending = self.check_pending(state)
        if pending is not None:
            return pending
        if self.actions_done >= cfg.max_actions_per_session:
            return "action_limit_reached"
        if state == "unknown":
            return "unknown_state: no click sent"
        if ai is None and cfg.require_ollama_for_actions:
            return "ollama_observation_missing: no click sent"
        if time.monotonic() - frame.captured_at > cfg.max_frame_age_seconds:
            return "stale_observation: no click sent"

        for action in actions:
            target = self._validate(action, frame.image, state, ai)
            if target is None or not action.postcondition_state:
                continue
            fresh, fresh_state = refresh()
            if fresh_state != state or fresh.window.id != frame.window.id:
                return "screen_changed: no click sent"
            verified = self._validate(action, fresh.image, fresh_state, ai)
            if verified is None:
                return "target_changed: no click sent"
            score, point = verified
            if max(abs(a - b) for a, b in zip(point, target[1])) > 8:
                return "target_moved: no click sent"

            def before_click() -> None:
                now = time.monotonic()
                if (now - frame.captured_at > cfg.max_frame_age_seconds
                        or now - fresh.captured_at > cfg.max_frame_age_seconds):
                    raise RuntimeError("Frame expired before click.")
                guard(fresh)

            desktop = (point[0] + fresh.origin[0], point[1] + fresh.origin[1])
            # Reserve the action before dispatch: partial input failures must
            # never cause an automatic duplicate click.
            self.halted = True
            self.clicker.click(*desktop, before_click=before_click)
            self.actions_done += 1
            now = time.monotonic()
            ready_at = now + max(0.0, action.cooldown_seconds)
            self.pending = (action, ready_at,
                            ready_at + cfg.postcondition_timeout_seconds)
            self.halted = False
            self.log("action", name=action.name, state=state, score=score,
                     point=desktop, conditions=list(action.conditions))
            return f"clicked:{action.name}"
        return "no eligible action"

    def _validate(self, action: ActionRule, img: np.ndarray, state: str,
                  ai: dict | None) -> tuple[float, tuple[int, int]] | None:
        if not action.enabled or action.requires_manual_review:
            return None
        if state not in action.when_state or not action.template:
            return None

        if action.conditions:
            if ai is None:
                return None
            if not all(condition_matches(c, ai) for c in action.conditions):
                return None

        score, point = self.matcher.match(img, action.template)
        minimum = (
            action.min_confidence
            if action.min_confidence is not None
            else self.cfg.min_confidence_for_action
        )
        if point is None or score < minimum:
            return None

        h, w = img.shape[:2]
        x, y = point
        if not (EDGE_MARGIN <= x < w - EDGE_MARGIN
                and EDGE_MARGIN <= y < h - EDGE_MARGIN):
            return None

        return score, point
