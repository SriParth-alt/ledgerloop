"""Provider-agnostic LLM adapter.

One ``complete(prompt) -> str`` interface, with a real provider selected by env var and
``temperature=0`` always.

The adapter is itself a talking point: no vendor lock-in, and swapping providers is a
config change rather than a refactor. It is also the seam that makes Tier 3 testable —
every gate failure in `tests/test_gates.py` is driven by a scripted response, which is a
better instrument than a real model. You cannot ask a real model to fabricate an
identifier on cue, and §7.3 requires exactly that failure to be demonstrated.

**The HTTP adapter below is exercised by no test.** It cannot be: running it costs money
and requires a key. Its first real call will be a human's, and that is stated here rather
than left for someone to discover.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

#: Reconciliation must be reproducible for audit (§7.4). Anything above zero would make
#: two runs of the same batch disagree about which money was matched.
TEMPERATURE = 0.0

DEFAULT_MODEL = "claude-sonnet-4-5"
REQUEST_TIMEOUT_SECONDS = 30.0


class LLMAdapter(Protocol):
    """What Tier 3 needs from a model, and nothing else."""

    name: str

    def complete(self, prompt: str) -> str: ...


@dataclass
class ScriptedAdapter:
    """Returns prepared responses in order. Tests and the live hallucination demo.

    ``calls`` is public so a test can assert that a cached run made **zero** calls —
    §7.4's promise is about API calls not happening, which is not observable from the
    match set alone.
    """

    responses: list[str] = field(default_factory=list)
    name: str = "scripted"
    calls: int = 0

    def complete(self, prompt: str) -> str:
        del prompt
        self.calls += 1
        if not self.responses:
            raise AssertionError(
                "ScriptedAdapter ran out of responses — the code under test called the "
                "model more times than the test expected"
            )
        return self.responses.pop(0)


@dataclass
class AnthropicAdapter:
    """Anthropic Messages API. **Not exercised by any test.**

    Kept deliberately thin. The adapter's job is to return the model's text and nothing
    more — no retries that coax a parseable answer, no repair, no fallback model. Those
    all belong to the class of behaviour §7.3 forbids, and putting them here would hide
    them from the gates.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    name: str = field(init=False)

    def __post_init__(self) -> None:
        self.name = self.model

    def complete(self, prompt: str) -> str:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": self.model,
                "max_tokens": 1024,
                "temperature": TEMPERATURE,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return "".join(part.get("text", "") for part in body.get("content", []))


def build_adapter(
    *, api_key: str | None = None, model: str = DEFAULT_MODEL
) -> LLMAdapter | None:
    """Build an adapter, or return ``None`` when no model is reachable.

    ``None`` is a first-class outcome rather than an error. §8 requires the batch to
    complete without Tier 3 when the model is unavailable — auto-match rate falls,
    correctness does not — so "no adapter" has to be something the cascade can carry
    rather than something it raises on.
    """
    key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return None
    return AnthropicAdapter(api_key=key, model=model)


def payload_fingerprint(prompt: str, *, model: str) -> str:
    """Stable identity for a prompt and the model it was sent to.

    The model name is part of the key deliberately: the same prompt answered by a
    different model is a different answer, and serving one from the other's cache would
    make the provenance record's ``model_name`` a lie.
    """
    import hashlib

    return hashlib.sha256(json.dumps({"model": model, "prompt": prompt}).encode()).hexdigest()
