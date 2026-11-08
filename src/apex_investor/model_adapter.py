"""Model adapters for the M3a quality gate (Phase 2 / M3a).

Two adapters behind one protocol:
  - `RecordedAdapter` — OFFLINE. Returns pre-recorded responses (used by every test). No network.
  - `LiveAdapter` — the ONLY live-network surface. It imports the network library LAZILY inside the
    call, so importing this module (and the core gate) never pulls in a network stack. It is used
    only by the operator-run CLI, never by tests.

The adapter is responsible for a single scenario call and returns either raw text or raises
`AdapterError`. The core gate (`model_gate.py`) never imports this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class AdapterError(Exception):
    """Raised when an adapter cannot produce a response (network, auth, timeout)."""


class ModelAdapter(Protocol):
    def complete(self, *, system: str, user: str) -> str:  # pragma: no cover - protocol
        ...


@dataclass(frozen=True)
class RecordedAdapter:
    """Offline adapter: returns `responses[key]` for a requested key.

    `key` is the scenario id. A missing key raises `AdapterError` so a missing response is visible
    (it must never silently become an empty finding list).
    """

    responses: dict

    def complete(self, *, key: str, system: str = "", user: str = "") -> str:
        if key not in self.responses:
            raise AdapterError(f"no recorded response for {key!r}")
        value = self.responses[key]
        if value is None:
            raise AdapterError(f"recorded response for {key!r} is null")
        return value


class LiveAdapter:
    """Operator-only live adapter. The network import is deferred to the call.

    Deliberately minimal: it POSTs a chat completion request to an OpenAI-compatible endpoint
    (e.g. OpenRouter) and returns the assistant text. It reads its key from the environment; it
    NEVER hardcodes or logs a credential. Tests never instantiate this.
    """

    def __init__(self, *, model: str, endpoint: str, api_key_env: str = "OPENROUTER_API_KEY",
                 timeout: int = 60, temperature: float = 0.0) -> None:
        self.model = model
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.timeout = timeout
        self.temperature = temperature

    def complete(self, *, key: str = "", system: str = "", user: str = "") -> str:
        import json  # local, cheap
        import os
        import urllib.request  # LAZY: keeps the network stack out of import-time

        api_key = os.environ.get(self.api_key_env)
        if not api_key:
            raise AdapterError(f"missing credential in environment variable {self.api_key_env}")
        body = json.dumps({
            "model": self.model,
            "temperature": self.temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface any transport failure as AdapterError
            raise AdapterError(f"live request failed: {type(exc).__name__}") from exc
        try:
            return payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise AdapterError("unexpected response shape from provider") from exc
