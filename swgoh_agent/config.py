"""Typed access to config.json."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_WINDOW_TITLE = "Star Wars: Galaxy of Heroes"


@dataclass(frozen=True)
class CaptureSettings:
    output: Path
    timeout_seconds: int
    window_title: str


@dataclass(frozen=True)
class InputSettings:
    enabled: bool
    click_button: str
    min_seconds_between_clicks: float


@dataclass(frozen=True)
class OllamaSettings:
    enabled: bool
    url: str
    model: str
    timeout_seconds: int
    num_predict: int
    send_image: bool


@dataclass(frozen=True)
class AgentSettings:
    dry_run: bool
    loop_interval_seconds: float
    max_consecutive_errors: int
    template_threshold: float
    min_confidence_for_action: float
    max_actions_per_session: int
    require_ollama_for_actions: bool
    max_frame_age_seconds: float = 30.0
    postcondition_timeout_seconds: float = 30.0


@dataclass(frozen=True)
class Settings:
    resolution: tuple[int, int]
    capture: CaptureSettings
    input: InputSettings
    ollama: OllamaSettings
    agent: AgentSettings
    templates: dict[str, str]
    raw: dict[str, Any]  # kept for the one-time legacy import

    @classmethod
    def load(cls, path: Path = ROOT / "config.json") -> "Settings":
        raw = json.loads(path.read_text(encoding="utf-8"))
        cap = raw["capture"]
        inp = raw.get("input", {})
        oll = raw.get("ollama", {})
        ag = raw.get("agent", {})

        ollama = OllamaSettings(
            enabled=bool(oll.get("enabled", False)),
            url=str(oll.get("url", "")).rstrip("/"),
            model=str(oll.get("model", "")),
            timeout_seconds=int(oll.get("timeout_seconds", 180)),
            num_predict=int(oll.get("num_predict", 400)),
            send_image=bool(oll.get("send_image", True)),
        )
        # Fail fast instead of logging an ollama_error on every loop.
        if ollama.enabled and not ollama.url.endswith("/api/chat"):
            raise ValueError(
                f"Expected Ollama /api/chat endpoint, got: {ollama.url}"
            )

        return cls(
            resolution=tuple(raw["resolution"]),
            capture=CaptureSettings(
                output=ROOT / cap["output"],
                timeout_seconds=int(cap["timeout_seconds"]),
                window_title=cap.get("window_title", DEFAULT_WINDOW_TITLE),
            ),
            input=InputSettings(
                enabled=bool(inp.get("enabled", False)),
                click_button=str(inp.get("click_button", "0xC0")),
                min_seconds_between_clicks=float(
                    inp.get("min_seconds_between_clicks", 2.0)
                ),
            ),
            ollama=ollama,
            agent=AgentSettings(
                dry_run=bool(ag.get("dry_run", True)),
                loop_interval_seconds=float(ag.get("loop_interval_seconds", 3)),
                max_consecutive_errors=int(ag.get("max_consecutive_errors", 5)),
                template_threshold=float(ag.get("template_threshold", 0.80)),
                min_confidence_for_action=float(
                    ag.get("min_confidence_for_action", 0.94)
                ),
                max_actions_per_session=int(
                    ag.get("max_actions_per_session", 20)
                ),
                max_frame_age_seconds=float(ag.get("max_frame_age_seconds", 30)),
                postcondition_timeout_seconds=float(ag.get("postcondition_timeout_seconds", 30)),
                require_ollama_for_actions=bool(
                    ag.get("require_ollama_for_actions", False)
                ),
            ),
            templates=dict(raw.get("templates", {})),
            raw=raw,
        )

    def resolve_template(self, ref: str | None) -> Path | None:
        """Resolve a template key or a relative/absolute path."""
        if not ref:
            return None
        path = Path(self.templates.get(ref, ref))
        return path if path.is_absolute() else ROOT / path
