"""Provider-agnostic LLM adapter.

One ``complete(prompt) -> str`` interface, with the provider selected by env var.

The adapter is itself a talking point: no vendor lock-in, and swapping providers is a
config change rather than a refactor. That claim was untested until day 12, when the
project moved from Anthropic to Gemini for cost reasons and **nothing above this file
moved** — not the tiers, not the gates, not the cache, not the orchestrator. A second
real implementation appearing without disturbing its callers is the evidence; ADR-031
records it.

It is also the seam that makes Tier 3 testable — every gate failure in
`tests/test_gates.py` is driven by a scripted response, which is a better instrument than
a real model. You cannot ask a real model to fabricate an identifier on cue, and §7.3
requires exactly that failure to be demonstrated.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

#: Reconciliation must be reproducible for audit (§7.4). Anything above zero would make
#: two runs of the same batch disagree about which money was matched.
#:
#: This is belt to the cache's braces, and only the cache is load-bearing: a re-run of
#: the same fixture never reaches the provider at all. Temperature matters for the
#: *first* call on a regenerated fixture, which is a cache miss by construction.
TEMPERATURE = 0.0

#: Pinned so a re-run breaks ties the same way. Free with `temperature=0`, and it costs
#: nothing to remove one more source of drift from an auditable number.
GENERATION_SEED = 42

#: Enough for a whole adjudication. A truncated response fails the schema gate, and we
#: would be measuring our own ceiling rather than the model's judgement.
MAX_OUTPUT_TOKENS = 2048

#: Thinking tokens bill as output and count against tokens-per-minute. Tier 3 picks from
#: a short ranked list against arithmetic that Python re-checks anyway, so paying for
#: extended reasoning buys nothing the gates would trust.
THINKING_LEVEL = "low"

REQUEST_TIMEOUT_SECONDS = 30.0

#: `gemini-2.5-flash-lite` is retired for new keys — it returns 404 with a message
#: naming this as the successor. Found by making one validation call before committing to
#: a 677-call sweep; the whole run would have died on request one.
#:
#: The model name is part of the cache key, so changing this constant invalidates every
#: committed response and silently starts spending quota again. Change it deliberately.
GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"

ANTHROPIC_DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MODEL = GEMINI_DEFAULT_MODEL

#: Bounded so an unattended sweep cannot spend a day retrying a quota that is gone.
MAX_ATTEMPTS = 5
BACKOFF_BASE_SECONDS = 2.0


class LLMAdapter(Protocol):
    """What Tier 3 needs from a model, and nothing else."""

    name: str

    def complete(self, prompt: str) -> str: ...


class SweepInterruptedError(RuntimeError):
    """The sweep could not finish, and no number should be reported from it.

    A first-class outcome, not an exceptional one. Raising is what lets the runner stop
    cleanly with the cache warm and resume later. The alternative — returning a
    placeholder — would write an answer the model never gave into a cache we treat as
    evidence.

    Deliberately broader than a quota error. The first real sweep died on a **503**, not
    a 429: the retry predicate only recognised throttling, so a transient outage
    propagated and killed a run 58 calls in. Both conditions mean the same thing to the
    caller — stop, keep what you bought, come back — so they share a base rather than
    forcing every caller to enumerate provider failure modes.
    """


class DailyQuotaExhaustedError(SweepInterruptedError):
    """The day's requests are gone.

    Distinguished from its base because it is the one interruption that will not clear
    by waiting a minute: the free tier allows 500 requests a day and §9.2 needs 677, so
    this is expected roughly once per full sweep.
    """


@dataclass(frozen=True)
class RateLimit:
    """Provider ceilings, as measured on the account actually being used.

    Defaults are the AI Studio free tier for `gemini-3.5-flash-lite`, read off the
    dashboard rather than the docs, which no longer publish them.
    """

    requests_per_minute: int = 15
    requests_per_day: int = 500


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


#: Status codes worth trying again. 429 is throttling; 5xx is the provider having a
#: bad moment. Neither is a verdict about the credit being adjudicated, and treating
#: either as one would let infrastructure noise masquerade as a finding.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def _is_retryable(error: Exception) -> bool:
    """Recognise a transient failure by status code, not by exception class.

    By code rather than class because an SDK rename would otherwise turn a retryable
    error into a crashed sweep. Learned the expensive way: the first real run stopped
    after 58 calls on a `ServerError: 503`, because this function only knew about 429.
    """
    code = getattr(error, "code", None)
    if code in RETRYABLE_STATUS:
        return True
    text = str(error)
    return "RESOURCE_EXHAUSTED" in text or any(
        str(status) in text for status in RETRYABLE_STATUS
    )


@dataclass
class GeminiAdapter:
    """Google AI Studio, via the official ``google-genai`` SDK.

    Built for a free tier rather than around it. Three behaviours carry that weight:

    * **Spacing.** Requests are held to ``requests_per_minute``. Firing as fast as the
      loop allows earns a 429 on request sixteen and converts a throughput limit into a
      failure.
    * **Retry, bounded.** A 429 means "not now", not "no". Letting it propagate would
      strand the credit with no adjudication, and the orchestrator would sweep it into
      the queue as though the model had declined — a rate limit masquerading as a
      finding. But the retries are capped, because an unattended run that loops forever
      burns a day and produces nothing.
    * **Exhaustion is loud.** ``DailyQuotaExhaustedError`` stops the sweep so the caller can
      leave the cache warm and resume tomorrow.

    ``client`` and ``sleep`` are injectable so the whole of the above is testable without
    a key, a network, or a wall-clock wait.
    """

    api_key: str
    model: str = GEMINI_DEFAULT_MODEL
    limit: RateLimit = field(default_factory=RateLimit)
    client: Any | None = None
    sleep: Callable[[float], None] = time.sleep
    name: str = field(init=False)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    _last_started_at: float | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.name = self.model

    # -- plumbing -------------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Built on first use so importing this module never requires the SDK.

        §8 makes "no model reachable" a state the cascade carries. A package that cannot
        be imported without a provider library would make Tiers 0-2 depend on Tier 3's
        dependencies, which is exactly backwards.
        """
        if self.client is None:
            from google import genai

            self.client = genai.Client(api_key=self.api_key)
        return self.client

    def _config(self) -> Any:
        from google.genai import types

        return types.GenerateContentConfig(
            temperature=TEMPERATURE,
            seed=GENERATION_SEED,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
        )

    def _wait_for_slot(self) -> None:
        interval = 60.0 / max(self.limit.requests_per_minute, 1)
        if self._last_started_at is not None:
            elapsed = time.monotonic() - self._last_started_at
            if elapsed < interval:
                self.sleep(interval - elapsed)
        self._last_started_at = time.monotonic()

    def _record_usage(self, response: Any) -> None:
        """Read what the provider says it billed.

        §9.2 asks for cost per 100 records. Deriving it from prompt length would be a
        guess wearing a number's clothes. Missing metadata costs us the cost row and
        nothing else — never the answers already paid for.
        """
        usage = getattr(response, "usage_metadata", None)
        if usage is None:
            return
        self.input_tokens += getattr(usage, "prompt_token_count", 0) or 0
        self.output_tokens += getattr(usage, "candidates_token_count", 0) or 0

    # -- the interface --------------------------------------------------------

    def complete(self, prompt: str) -> str:
        if self.calls >= self.limit.requests_per_day:
            raise DailyQuotaExhaustedError(
                f"{self.calls} requests made against a daily limit of "
                f"{self.limit.requests_per_day}. The cache holds every answer already "
                f"paid for; re-run tomorrow to continue."
            )

        client = self._ensure_client()
        config = self._config()
        last: Exception | None = None

        for attempt in range(MAX_ATTEMPTS):
            self._wait_for_slot()
            try:
                response = client.models.generate_content(
                    model=self.model, contents=prompt, config=config
                )
            except Exception as error:
                if not _is_retryable(error):
                    raise
                last = error
                self.sleep(BACKOFF_BASE_SECONDS * (2**attempt))
                continue

            # One logical call, however many attempts it took. Counting attempts would
            # make the reported call count a measure of the network rather than of how
            # often the cascade needed to ask.
            self.calls += 1
            self._record_usage(response)
            return response.text or ""

        raise SweepInterruptedError(
            f"gave up after {MAX_ATTEMPTS} attempts ({last}). The cache holds every "
            f"answer already paid for; re-run to continue."
        )


@dataclass
class AnthropicAdapter:
    """Anthropic Messages API. **Not exercised by any test.**

    Kept as the second implementation of the Protocol — the evidence that swapping
    providers touches nothing above this file. It is not the provider in use: the project
    moved to Gemini because a free tier was a hard requirement (ADR-031).

    Note the absent ``temperature``. Anthropic removed sampling parameters on current
    models and rejects them with a 400, so the determinism argument that once lived here
    now rests entirely on the response cache. Reviewed by inspection, never by a call.

    Kept deliberately thin. The adapter's job is to return the model's text and nothing
    more — no retries that coax a parseable answer, no repair, no fallback model. Those
    all belong to the class of behaviour §7.3 forbids, and putting them here would hide
    them from the gates.
    """

    api_key: str
    model: str = ANTHROPIC_DEFAULT_MODEL
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
                "max_tokens": MAX_OUTPUT_TOKENS,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        body = response.json()
        return "".join(part.get("text", "") for part in body.get("content", []))


def api_key_from_environment(provider: str = "gemini") -> str | None:
    """Both spellings, because AI Studio's own documentation uses both.

    A key that is present but unread is the most annoying possible failure, and it looks
    identical to having no key at all.
    """
    if provider == "anthropic":
        return os.environ.get("ANTHROPIC_API_KEY")
    return os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")


def build_adapter(
    *,
    provider: str = "gemini",
    api_key: str | None = None,
    model: str | None = None,
    limit: RateLimit | None = None,
) -> LLMAdapter | None:
    """Build an adapter, or return ``None`` when no model is reachable.

    ``None`` is a first-class outcome rather than an error. §8 requires the batch to
    complete without Tier 3 when the model is unavailable — auto-match rate falls,
    correctness does not — so "no adapter" has to be something the cascade can carry
    rather than something it raises on.
    """
    key = api_key or api_key_from_environment(provider)
    if not key:
        return None
    if provider == "anthropic":
        return AnthropicAdapter(api_key=key, model=model or ANTHROPIC_DEFAULT_MODEL)
    return GeminiAdapter(
        api_key=key,
        model=model or GEMINI_DEFAULT_MODEL,
        limit=limit or RateLimit(),
    )
