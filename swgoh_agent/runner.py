"""Main observe -> detect -> (maybe) act loop."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import time

from .actions import ActionExecutor, Clicker
from .action_types import INPUT_KINDS, SENT_PREFIXES
from .capture import WindowCapture
from .config import ROOT, Settings
from .eventlog import EventLog
from .ollama import OllamaClient
from .store import Store
from .runtime import AgentLease
from .vision import TemplateMatcher, detect_state


class Agent:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.log = EventLog(ROOT / "logs" / "agent.jsonl")
        self.store = Store(settings.raw)
        self.capture = WindowCapture.from_settings(settings)
        self.matcher = TemplateMatcher(settings.resolve_template, settings.template_preprocessing)
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
            self.store.save_latest_state({"state": state, "scores": scores,
                                          "confidence": confidence,
                                          "actions_done": self.executor.actions_done,
                                          "action_result": pending})
            return
        def refresh():
            fresh = self.capture.grab()
            detected = detect_state(fresh.image, rules.states, self.matcher,
                                    self.settings.agent.template_threshold)
            return fresh, detected.state

        def execute(ai):
            return self.executor.run(
                frame, state, ai, rules.actions, refresh=refresh,
                guard=self.capture.assert_click_target,
                prepare=(self.capture.focus_window
                         if self.settings.input.enabled and self.settings.input.auto_focus
                         else None),
            )

        # Preserve priority: if any eligible rule needs AI conditions, wait for
        # the model. Otherwise AI-independent input must not wait for it.
        clicks = [a for a in rules.actions if a.kind in INPUT_KINDS and a.enabled
                  and not a.requires_manual_review and state in a.when_state]
        early_result = None
        if (clicks and not self.executor.cfg.require_ollama_for_actions
                and not any(a.conditions for a in clicks)):
            early_result = execute(None)

        ai = None
        if early_result is None or not early_result.startswith((*SENT_PREFIXES, "click_deferred:")):
            ai = self.ollama.observe(frame.image, state, scores, rules)
            self.executor.observe(state, ai, rules.actions)

        action_result = early_result if early_result is not None else execute(ai)
        result = {
            "state": state,
            "confidence": confidence,
            "scores": scores,
            "ai": ai,
            "actions_done": self.executor.actions_done,
            "action_result": action_result,
        }
        self.store.save_latest_state(result)
        self.log("observation", **result)
        print(f"State={state} confidence={confidence:.3f} scores={scores}")
        if ai:
            print("AI:", json.dumps(ai, ensure_ascii=False))
        print("Action:", action_result)

    def validate_templates(self, rules) -> None:
        missing = []
        for kind, definitions in (("state", rules.states), ("action", rules.actions)):
            for definition in definitions:
                if kind == "action" and definition.kind != "click":
                    continue
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

    def run(self, once: bool = False) -> None:
        try:
            with AgentLease(ROOT / "logs" / "agent.lock"):
                self._run(once)
        except KeyboardInterrupt:
            print("\nStopped by user.")

    def _run(self, once: bool = False) -> None:
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
                    if once:
                        # A single click still needs its postcondition checked;
                        # never start a second action while doing so.
                        while self.executor.pending and not self.executor.halted:
                            time.sleep(0.5)
                            frame = self.capture.grab()
                            rules = self.store.load_rules()
                            detected = detect_state(frame.image, rules.states, self.matcher,
                                                    s.agent.template_threshold)
                            status = self.executor.check_pending(detected.state)
                            self.log("action_status", state=detected.state, result=status)
                            self.store.save_latest_state({
                                "state": detected.state, "scores": detected.scores,
                                "confidence": detected.confidence,
                                "actions_done": self.executor.actions_done,
                                "action_result": status,
                            })
                            print("Action:", status)
                        return
                    errors = 0
                except Exception as exc:
                    errors += 1
                    self.log("error", error=str(exc), count=errors)
                    limit = s.agent.max_consecutive_errors
                    print(f"Error {errors}/{limit}: {exc}")
                    if once or errors >= limit:
                        print("Too many errors; stopping safely.")
                        return
                time.sleep(max(
                    0.0,
                    s.agent.loop_interval_seconds - (time.monotonic() - started),
                ))
        except KeyboardInterrupt:  # also covers Ctrl+C during the sleep
            print("\nStopped by user.")


def main() -> None:
    parser = argparse.ArgumentParser(description="SWGOH screen agent")
    parser.add_argument("--once", action="store_true", help="One step and postcondition verification")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Observe without input")
    mode.add_argument("--live", action="store_true", help="Enable guarded input for this process")
    args = parser.parse_args()
    settings = Settings.load()
    if args.dry_run or args.live:
        settings = replace(settings,
                           agent=replace(settings.agent, dry_run=not args.live),
                           input=replace(settings.input, enabled=args.live))
    Agent(settings).run(once=args.once)
