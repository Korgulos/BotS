"""Safety-gated action execution."""
from __future__ import annotations

import operator
import subprocess
import time
from typing import Any, Callable, Sequence

import numpy as np

from .config import AgentSettings, InputSettings
from .capture import ClickNotReady, Frame
from .store import ActionRule
from .vision import TemplateMatcher
from .textvision import TextMatcher
from .action_types import INPUT_KINDS, parameters, within_frame

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
    """Verified X11 pointer positioning with ydotool button input."""

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
        self._move(x, y)
        before_click()
        subprocess.run(
            ["ydotool", "click", self.cfg.click_button],
            check=True, timeout=5,
        )
        self._last_click = time.monotonic()

    def _ready(self, guard):
        if not self.cfg.enabled:
            raise RuntimeError("Input backend disabled in config.json.")
        wait = self.cfg.min_seconds_between_clicks - (time.monotonic()-self._last_click)
        if wait > 0:
            time.sleep(wait)
        guard()

    def _move(self, x, y, settle=0.25):
        if self.cfg.mousemove_backend == "xdotool":
            move = ["xdotool", "mousemove", "--sync", str(x), str(y)]
        else:
            move = ["ydotool", "mousemove", "--absolute", "-x", str(x), "-y", str(y)]
        subprocess.run(move, check=True, timeout=5)
        if settle:
            time.sleep(settle)
        # A successful command does not prove the pointer reached the target,
        # especially with ydotool acceleration and multiple Wayland monitors.
        try:
            position = subprocess.run(
                ["xdotool", "getmouselocation", "--shell"],
                check=True, timeout=5, capture_output=True, text=True,
            )
            fields = dict(line.split("=", 1) for line in position.stdout.splitlines()
                          if "=" in line)
            actual = (int(fields["X"]), int(fields["Y"]))
        except (OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
            raise ClickNotReady(f"Cannot verify pointer position: {exc}") from exc
        if max(abs(actual[0] - x), abs(actual[1] - y)) > 3:
            raise ClickNotReady(f"Pointer missed target: expected={(x, y)}, actual={actual}")

    def key(self, keys, hold_ms, before_click):
        self._ready(before_click)
        try:
            subprocess.run(['xdotool', 'keydown', keys], check=True, timeout=5)
            time.sleep(hold_ms/1000)
        finally:
            subprocess.run(['xdotool', 'keyup', keys], check=True, timeout=5)
        self._last_click = time.monotonic()

    def type_text(self, text, delay_ms, before_click):
        self._ready(before_click)
        # Small guarded batches limit accidental typing if focus is stolen.
        started = False
        try:
            for index in range(0, len(text), 8):
                if index:
                    before_click()
                chunk = text[index:index+8]
                started = True
                subprocess.run(['xdotool', 'type', '--delay', str(delay_ms), '--file', '-'],
                               input=chunk, text=True, check=True,
                               timeout=max(5, len(chunk)*delay_ms/1000+5))
        except ClickNotReady as exc:
            if started:
                raise RuntimeError(f'Text entry interrupted after partial input: {exc}') from exc
            raise
        self._last_click = time.monotonic()

    def drag(self, start, end, duration_ms, before_click):
        self._ready(before_click)
        self._move(*start)
        before_click()
        try:
            subprocess.run(['ydotool', 'click', '0x40'], check=True, timeout=5)
            steps = max(2, min(30, duration_ms // 50))
            for step in range(1, steps+1):
                before_click()
                point = tuple(round(a+(b-a)*step/steps) for a,b in zip(start,end))
                self._move(*point, settle=0)
                time.sleep(duration_ms/1000/steps)
            before_click()
        except ClickNotReady as exc:
            # After button-down, do not classify cancellation as a harmless
            # pre-input rejection: the game may already have scrolled.
            raise RuntimeError(f'Drag interrupted after button-down: {exc}') from exc
        finally:
            subprocess.run(['ydotool', 'click', '0x80'], check=True, timeout=5)
        self._last_click = time.monotonic()


class ActionExecutor:
    """Executes at most one action per call, and only if every check passes."""

    def __init__(self, cfg: AgentSettings, matcher: TemplateMatcher,
                 clicker: Clicker, log: Callable[..., None], text_matcher=None):
        self.cfg = cfg
        self.text_matcher = text_matcher or TextMatcher()
        self.matcher = matcher
        self.clicker = clicker
        self.log = log
        self.actions_done = 0
        self.pending: tuple[ActionRule, float, float] | None = None
        self.halted = False
        self.rejections: list[str] = []

    def check_pending(self, state: str) -> str | None:
        if self.halted:
            return "halted: restart after manual inspection"
        if self.pending is None:
            return None
        action, ready_at, deadline = self.pending
        now = time.monotonic()
        if now < ready_at:
            return f"cooldown:{action.name}"
        if state in action.expected_states:
            self.pending = None
            self.log("postcondition_confirmed", name=action.name, state=state)
            return f"confirmed:{action.name}"
        if now >= deadline:
            self.halted = True
            self.log("postcondition_failed", name=action.name,
                     expected=list(action.expected_states), state=state)
            return f"postcondition_failed:{action.name}; halted"
        return f"awaiting_postcondition:{action.name}"

    def observe(self, state: str, ai: dict | None,
                actions: Sequence[ActionRule]) -> None:
        """Observation rules only record model values; they never dispatch input."""
        if ai is None or not isinstance(ai.get("values"), dict):
            return
        for action in actions:
            if (action.kind != "observe" or not action.enabled
                    or action.requires_manual_review or state not in action.when_state):
                continue
            if not all(condition_matches(c, ai) for c in action.conditions):
                continue
            self.log("resource_observation", name=action.name, state=state,
                     values=ai["values"])

    def run(self, frame: Frame, state: str, ai: dict | None,
            actions: Sequence[ActionRule], *,
            refresh: Callable[[], tuple[Frame, str]],
            guard: Callable[[Frame], None],
            prepare: Callable[[Frame], None] | None = None) -> str:
        cfg = self.cfg
        self.rejections = []
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
            if target is None or not action.expected_states:
                continue
            if prepare is not None:
                try:
                    prepare(frame)
                except ClickNotReady as exc:
                    self.log("click_deferred", name=action.name, reason=str(exc))
                    return f"click_deferred:{exc}"
            fresh, fresh_state = refresh()
            if fresh_state != state or fresh.window.id != frame.window.id:
                return "screen_changed: no click sent"
            verified = self._validate(action, fresh.image, fresh_state, ai)
            if verified is None:
                return "target_changed: no click sent"
            score, point = verified
            if point is not None and target[1] is not None and max(abs(a - b) for a, b in zip(point, target[1])) > 8:
                return "target_moved: no click sent"

            def before_click() -> None:
                now = time.monotonic()
                if (now - frame.captured_at > cfg.max_frame_age_seconds
                        or now - fresh.captured_at > cfg.max_frame_age_seconds):
                    raise ClickNotReady("Frame expired before click.")
                guard(fresh)

            desktop = ((point[0] + fresh.origin[0], point[1] + fresh.origin[1])
                       if point is not None else None)
            params = parameters(action.kind, action.parameters)
            # Reserve the action before dispatch: partial input failures must
            # never cause an automatic duplicate click.
            self.halted = True
            try:
                if action.kind in ('click', 'click_text'):
                    self.clicker.click(*desktop, before_click=before_click)
                elif action.kind == 'key':
                    self.clicker.key(params['keys'], params['hold_ms'], before_click=before_click)
                elif action.kind == 'type_text':
                    self.clicker.type_text(params['text'], params['delay_ms'], before_click=before_click)
                elif action.kind == 'drag':
                    end = (params['end_x']+fresh.origin[0], params['end_y']+fresh.origin[1])
                    self.clicker.drag(desktop, end, params['duration_ms'], before_click=before_click)
            except ClickNotReady as exc:
                # Both Clicker guards run before the button command. Moving
                # the pointer alone does not make a cancelled click ambiguous.
                self.halted = False
                self.log("click_deferred", name=action.name, reason=str(exc))
                return f"click_deferred:{exc}"
            self.actions_done += 1
            now = time.monotonic()
            ready_at = now + max(0.0, action.cooldown_seconds)
            self.pending = (action, ready_at,
                            ready_at + cfg.postcondition_timeout_seconds)
            self.halted = False
            self.log("action", name=action.name, state=state, score=score,
                     point=desktop, conditions=list(action.conditions))
            prefix = {"click": "clicked", "click_text": "clicked", "key": "key_sent",
                      "type_text": "text_entered", "drag": "dragged"}[action.kind]
            return f"{prefix}:{action.name}"
        detail = "; ".join(self.rejections)
        return "no eligible action" + (f": {detail}" if detail else "")

    def _validate(self, action: ActionRule, img: np.ndarray, state: str,
                  ai: dict | None) -> tuple[float, tuple[int, int] | None] | None:
        if action.kind not in INPUT_KINDS or not action.enabled or action.requires_manual_review:
            return None
        if state not in action.when_state:
            return None

        if action.conditions:
            if ai is None:
                return None
            if not all(condition_matches(c, ai) for c in action.conditions):
                return None

        try:
            params = parameters(action.kind, action.parameters)
            within_frame(action.kind, params, img.shape[1], img.shape[0])
        except ValueError as exc:
            self.rejections.append(f'{action.name}: {exc}')
            return None
        if action.kind in ('key', 'type_text'):
            return 1.0, None
        if action.kind == 'drag':
            return 1.0, (params['start_x'], params['start_y'])
        if action.kind == 'click_text':
            match = self.text_matcher.match(img, params)
            score, point = match.score, match.point
            minimum = params['min_text_confidence']
            if match.reason:
                self.rejections.append(f'{action.name}: {match.reason}')
        else:
            if not action.template:
                return None
            score, point = self.matcher.match(img, action.template)
            minimum = (action.min_confidence if action.min_confidence is not None
                       else self.cfg.min_confidence_for_action)
        if point is None or score < minimum:
            score_label = 'ocr_score' if action.kind == 'click_text' else 'template_score'
            reason = (f"{action.name}: {score_label}={score:.3f}, "
                      f"required={minimum:.3f}, target={point}")
            self.rejections.append(reason)
            self.log("action_rejected", name=action.name,
                     reason=("ocr_below_threshold_or_missing" if action.kind == "click_text"
                             else "template_below_threshold_or_missing"), score=score,
                     required=minimum, point=point)
            return None

        h, w = img.shape[:2]
        x, y = point
        if not (EDGE_MARGIN <= x < w - EDGE_MARGIN
                and EDGE_MARGIN <= y < h - EDGE_MARGIN):
            return None

        return score, point
