"""Ollama vision client: asks the model to read values off a screenshot."""
from __future__ import annotations

import base64
import json
from string import Template
from typing import Callable

import cv2
import numpy as np
import requests

from .config import OllamaSettings
from .store import Rules

PROMPT_KEY = "visual_observation"


def build_prompt(rules: Rules, state: str, scores: dict) -> str:
    template = rules.prompts.get(PROMPT_KEY)
    if template is None:
        raise ValueError(f"Prompt not found in SQLite: {PROMPT_KEY}")
    fields = [
        {"name": f.name, "description": f.description, "type": f.type}
        for f in rules.observations
    ]
    return Template(template).substitute(
        state=state,
        template_scores_json=json.dumps(scores),
        requested_fields_json=json.dumps(fields, ensure_ascii=False),
    )


class OllamaClient:
    def __init__(self, cfg: OllamaSettings, log: Callable[..., None]):
        self.cfg = cfg
        self._log = log
        self._session = requests.Session()

    def observe(self, img: np.ndarray, state: str, scores: dict,
                rules: Rules) -> dict | None:
        """Return the model's JSON observation, or None if disabled/failed."""
        if not self.cfg.enabled:
            return None

        prompt = build_prompt(rules, state, scores)
        message: dict = {"role": "user", "content": prompt}

        try:
            if self.cfg.send_image:
                ok, encoded = cv2.imencode(".png", img)
                if not ok:
                    raise RuntimeError("Could not encode screenshot for Ollama.")
                message["images"] = [
                    base64.b64encode(encoded.tobytes()).decode("ascii")
                ]

            response = self._session.post(
                self.cfg.url,
                json={
                    "model": self.cfg.model,
                    "messages": [message],
                    "stream": False,
                    "format": "json",
                    "think": False,
                    "options": {
                        "temperature": 0,
                        "num_predict": self.cfg.num_predict,
                    },
                },
                timeout=(5, self.cfg.timeout_seconds),
            )
            response.raise_for_status()

            content = response.json().get("message", {}).get("content", "")
            if not content:
                raise ValueError("Ollama returned an empty response.")

            result = json.loads(content)
            if not isinstance(result, dict):
                raise ValueError("Ollama response must be a JSON object.")
            if not isinstance(result.get("values"), dict):
                result["values"] = {}
            return result

        except Exception as exc:
            self._log("ollama_error", error=str(exc))
            return None
