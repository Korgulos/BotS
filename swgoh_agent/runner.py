"""Main observe -> detect -> (maybe) act loop."""
from __future__ import annotations

import json
import time

from .actions import ActionExecutor, Clicker
from .capture import WindowCapture
from .config import ROOT, Settings
from .eventlog import EventLog
from .ollama import OllamaClient
from .store import Store
from .vision import TemplateMatcher, detect_state


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log = EventLog(ROOT / "logs" / "agent.jsonl")
        self.store = Store(settings.raw)
        self.capture = WindowCapture.from_settings(settings)
        self.matcher = TemplateMatcher(settings.resolve_template)
        self.ollama = OllamaClient(settings.ollama, self.log)
        self.executor = ActionExecutor(
            settings.agent, self.matcher, Clicker(settings.input), self.log
        )

    def step(self) -> None:
        # Re-read rules every iteration so DB edits apply without a restart.
        rules = self.store.load_rules()
        frame = self.capture.grab()
        state, scores, confidence = detect_state(
            frame.image, rules.states, self.matcher,
            self.settings.agent.template_threshold,
        )
        pending = self.executor.check_pending(state)
        if pending is not None:
            print("Action:", pending)
            self.log("action_status", state=state, result=pending)
            return
        ai = self.ollama.observe(frame.image, state, scores, rules)

        result = {
            "state": state,
            "confidence": confidence,
            "scores": scores,
            "ai": ai,
            "actions_done": self.executor.actions_done,
        }
        self.store.save_latest_state(result)
        self.log("observation", **result)

        print(f"State={state} confidence={confidence:.3f} scores={scores}")
        if ai:
            print("AI:", json.dumps(ai, ensure_ascii=False))

        def refresh():
            fresh = self.capture.grab()
            detected = detect_state(fresh.image, rules.states, self.matcher,
                                    self.settings.agent.template_threshold)
            return fresh, detected.state

        print("Action:", self.executor.run(
            frame, state, ai, rules.actions, refresh=refresh,
            guard=self.capture.assert_click_target,
        ))

    def validate_templates(self, rules) -> None:
        missing = []
        for kind, definitions in (("state", rules.states), ("action", rules.actions)):
            for definition in definitions:
                path = self.settings.resolve_template(definition.template)
                if not self.matcher.available(definition.template):
                    label = f"{kind}:{definition.name} -> {path}"
                    if kind == "state" or (definition.enabled and not definition.requires_manual_review):
                        missing.append(label)
                    self.log("missing_template", definition=label)
                    print("Missing/unreadable template:", label)
                    if (kind == "action" and definition.enabled
                            and not definition.requires_manual_review):
                        raise ValueError(f"Enabled action has no usable template: {label}")
        if not self.settings.agent.dry_run and missing:
            raise ValueError("Resolve missing templates before enabling live actions.")

    def run(self) -> None:
        self.store.initialize()
        rules = self.store.load_rules()
        s = self.settings
        self.validate_templates(rules)
        print("SWGOH Agent — SQLite-backed mode")
        print("Dry-run:", s.agent.dry_run)
        print("Input enabled:", s.input.enabled)
        print("Configured states:", [st.name for st in rules.states])
        print("Configured actions:", len(rules.actions))
        print("Ctrl+C stops the agent. Unknown state = no click.")

        errors = 0
        try:
            while True:
                started = time.monotonic()
                try:
                    self.step()
                    errors = 0
                except Exception as exc:
                    errors += 1
                    self.log("error", error=str(exc), count=errors)
                    limit = s.agent.max_consecutive_errors
                    print(f"Error {errors}/{limit}: {exc}")
                    if errors >= limit:
                        print("Too many errors; stopping safely.")
                        return
                time.sleep(max(
                    0.0,
                    s.agent.loop_interval_seconds - (time.monotonic() - started),
                ))
        except KeyboardInterrupt:  # also covers Ctrl+C during the sleep
            print("\nStopped by user.")


def main() -> None:
    Agent(Settings.load()).run()
