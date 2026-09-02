"""The Gemini adapter — Tier 3's first real model, and the first test of ADR-024.

ADR-024 claimed the ``LLMAdapter`` Protocol made the provider a config change rather
than a refactor. This file is where that claim stops being an assertion: a second real
implementation appears, and nothing above it moves.

**Not one test here makes a network call.** A fake client is injected, which is the same
argument `tests/test_gates.py` makes about ``ScriptedAdapter``: you cannot ask a real
model to return a 429 on cue, or to hand back malformed usage metadata, and those are
exactly the paths that decide whether a 677-call sweep survives contact with a free tier.
A test suite that needs a key and a quota is a test suite nobody runs.

The free tier's measured ceilings (AI Studio dashboard, `gemini-3.5-flash-lite`):
15 RPM, 250K TPM, **500 RPD**. The sweep needs 677 calls, so the daily cap is the binding
constraint and the adapter has to treat exhaustion as a first-class outcome rather than
an error to swallow. §9.2's rows are all-or-nothing: a partially-answered arm reports
*not yet measured*, never a number computed over the fraction that fit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from ledgerloop.llm.adapter import (
    GEMINI_DEFAULT_MODEL,
    DailyQuotaExhaustedError,
    GeminiAdapter,
    LLMAdapter,
    RateLimit,
    SweepInterruptedError,
    build_adapter,
)

# --- test doubles ------------------------------------------------------------------


@dataclass
class _Usage:
    prompt_token_count: int = 0
    candidates_token_count: int = 0


@dataclass
class _Response:
    text: str
    usage_metadata: _Usage = field(default_factory=_Usage)


class _RateLimitedError(Exception):
    """Stands in for the provider's 429.

    The adapter must recognise this by its status code rather than by its class, so that
    an SDK upgrade renaming the exception does not silently turn a retryable throttle
    into a crashed sweep.
    """

    code = 429


@dataclass
class _FakeModels:
    responses: list[Any]
    seen: list[dict[str, Any]] = field(default_factory=list)

    def generate_content(self, *, model: str, contents: str, config: Any) -> Any:
        self.seen.append({"model": model, "contents": contents, "config": config})
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


@dataclass
class _FakeClient:
    models: _FakeModels


def _client(*responses: Any) -> _FakeClient:
    return _FakeClient(models=_FakeModels(responses=list(responses)))


def _adapter(client: _FakeClient, **kwargs: Any) -> GeminiAdapter:
    """An adapter whose clock never really sleeps, so tests stay fast."""
    kwargs.setdefault("sleep", lambda _seconds: None)
    return GeminiAdapter(api_key="test-key", client=client, **kwargs)


# --- the Protocol claim ------------------------------------------------------------


def test_the_gemini_adapter_satisfies_the_same_protocol_as_every_other() -> None:
    """ADR-024's whole argument, in one assertion.

    Tier 3 depends on ``complete(prompt) -> str`` and a ``name``. If swapping providers
    required anything else, the 'no vendor lock-in' claim in the pitch would be a
    decoration rather than a property of the design.
    """
    adapter: LLMAdapter = _adapter(_client(_Response("{}")))

    assert adapter.name == GEMINI_DEFAULT_MODEL
    assert adapter.complete("prompt") == "{}"


def test_the_model_is_pinned_to_a_version_that_actually_exists() -> None:
    """`gemini-2.5-flash-lite` is retired for new keys and 404s.

    Pinned as a constant because the model name is part of the cache key: changing it
    silently invalidates every committed response, and a run that looks cached would
    quietly start spending quota again.
    """
    assert GEMINI_DEFAULT_MODEL == "gemini-3.5-flash-lite"


# --- determinism -------------------------------------------------------------------


def test_sampling_is_pinned_so_two_runs_of_one_batch_agree() -> None:
    """§7.4 requires a re-run to reproduce the same match set.

    Temperature alone leaves the provider free to break ties differently, so the seed is
    pinned too. Neither replaces the response cache — they make the *first* call of a
    re-generated fixture reproducible, which the cache cannot help with because a changed
    prompt is a cache miss by construction.
    """
    client = _client(_Response("{}"))
    _adapter(client).complete("prompt")

    config = client.models.seen[0]["config"]
    assert config.temperature == 0
    assert config.seed is not None


def test_the_reasoning_budget_is_held_down() -> None:
    """Thinking tokens bill as output and count against a 250K-per-minute ceiling.

    Tier 3 adjudicates a short candidate list against arithmetic that Python re-checks
    anyway. Paying for extended reasoning here buys nothing the gates would trust.
    """
    client = _client(_Response("{}"))
    _adapter(client).complete("prompt")

    config = client.models.seen[0]["config"]
    assert config.thinking_config is not None
    # The SDK coerces the string into a ThinkingLevel enum, so compare the value.
    level = config.thinking_config.thinking_level
    assert str(getattr(level, "value", level)).lower() == "low"


def test_the_output_ceiling_leaves_room_for_a_whole_adjudication() -> None:
    """A truncated response fails the schema gate, and we would be measuring our own
    ceiling rather than the model's judgement."""
    client = _client(_Response("{}"))
    _adapter(client).complete("prompt")

    assert client.models.seen[0]["config"].max_output_tokens >= 2048


# --- cost accounting ---------------------------------------------------------------


def test_token_usage_is_accumulated_from_the_provider() -> None:
    """§9.2 wants cost per 100 records. Estimating it from prompt length would be a
    guess wearing a number's clothes; the provider reports what it actually billed."""
    client = _client(
        _Response("a", _Usage(prompt_token_count=600, candidates_token_count=40)),
        _Response("b", _Usage(prompt_token_count=650, candidates_token_count=35)),
    )
    adapter = _adapter(client)
    adapter.complete("one")
    adapter.complete("two")

    assert adapter.input_tokens == 1250
    assert adapter.output_tokens == 75
    assert adapter.calls == 2


def test_missing_usage_metadata_does_not_crash_the_sweep() -> None:
    """Losing the cost row is a bad outcome. Losing 400 cached answers because the cost
    row could not be computed is a much worse one."""
    response = _Response("a")
    response.usage_metadata = None  # type: ignore[assignment]
    adapter = _adapter(_client(response))

    assert adapter.complete("one") == "a"
    assert adapter.input_tokens == 0


# --- surviving the free tier -------------------------------------------------------


def test_requests_are_spaced_to_stay_under_the_per_minute_ceiling() -> None:
    """15 RPM measured. Firing 677 requests as fast as the loop allows earns a 429 on
    request 16 and turns a throughput limit into a failure."""
    slept: list[float] = []
    client = _client(_Response("a"), _Response("b"), _Response("c"))
    adapter = _adapter(
        client,
        limit=RateLimit(requests_per_minute=15, requests_per_day=500),
        sleep=slept.append,
    )

    adapter.complete("one")
    adapter.complete("two")
    adapter.complete("three")

    # Four seconds between starts is 15 per minute. The adapter sleeps the interval
    # *minus time already spent*, so the figure lands just under it — asserting a hard
    # floor would be measuring clock precision rather than the spacing.
    assert slept
    assert all(delay == pytest.approx(60 / 15, abs=0.1) for delay in slept)


def test_a_throttle_is_retried_rather_than_treated_as_a_verdict() -> None:
    """A 429 says 'not now', not 'no'.

    Raising an exception here would strand the credit with no adjudication, which the
    orchestrator would sweep into the queue as though the model had declined it. That
    would be a rate limit masquerading as a finding.
    """
    client = _client(_RateLimitedError(), _RateLimitedError(), _Response("finally"))
    adapter = _adapter(client)

    assert adapter.complete("prompt") == "finally"
    assert adapter.calls == 1  # one logical call, however many attempts it took


def test_backoff_grows_rather_than_hammering_the_endpoint() -> None:
    slept: list[float] = []
    client = _client(_RateLimitedError(), _RateLimitedError(), _Response("ok"))
    adapter = _adapter(client, sleep=slept.append)
    adapter.complete("prompt")

    backoffs = [d for d in slept if d > 0]
    assert len(backoffs) >= 2
    assert backoffs[1] > backoffs[0]


def test_the_daily_cap_stops_the_sweep_loudly() -> None:
    """500 RPD against 677 needed calls: exhaustion is expected, not exceptional.

    It must raise something the runner can catch and act on — stop cleanly, leave the
    cache warm, resume tomorrow. Returning a placeholder string would poison the cache
    with an answer the model never gave.
    """
    client = _client(*[_Response("a") for _ in range(3)])
    adapter = _adapter(client, limit=RateLimit(requests_per_minute=15, requests_per_day=2))

    adapter.complete("one")
    adapter.complete("two")
    with pytest.raises(DailyQuotaExhaustedError):
        adapter.complete("three")


def test_persistent_throttling_gives_up_instead_of_looping_forever() -> None:
    """An unattended sweep that retries indefinitely is a way to burn a day and produce
    nothing. Bounded attempts, then surface it."""
    client = _client(*[_RateLimitedError() for _ in range(20)])
    adapter = _adapter(client)

    with pytest.raises(SweepInterruptedError):
        adapter.complete("prompt")


class _UnavailableError(Exception):
    """The provider having a bad moment. Not a verdict about this credit."""

    code = 503


def test_a_transient_server_error_is_retried_not_fatal() -> None:
    """The regression that killed the first real sweep.

    58 calls in, a `ServerError: 503` propagated through a retry predicate that only
    knew about 429 and took the whole run down. A 5xx says nothing about the credit
    being adjudicated, and letting it escape turns provider noise into a lost sweep.
    """
    client = _client(_UnavailableError(), _UnavailableError(), _Response("recovered"))
    adapter = _adapter(client)

    assert adapter.complete("prompt") == "recovered"
    assert adapter.calls == 1


def test_a_quota_cap_is_distinguishable_from_an_outage() -> None:
    """Both stop the sweep, but only one clears by waiting a minute. A caller deciding
    whether to resume now or tomorrow needs to tell them apart."""
    adapter = _adapter(
        _client(_Response("a")), limit=RateLimit(requests_per_minute=15, requests_per_day=0)
    )

    with pytest.raises(DailyQuotaExhaustedError):
        adapter.complete("prompt")
    assert issubclass(DailyQuotaExhaustedError, SweepInterruptedError)


# --- wiring ------------------------------------------------------------------------


@pytest.mark.parametrize("variable", ["GEMINI_API_KEY", "GOOGLE_API_KEY"])
def test_either_environment_variable_names_the_key(
    variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AI Studio's own docs use both spellings, and a key that is present but unread is
    the most annoying possible failure."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv(variable, "a-key")

    adapter = build_adapter(provider="gemini")

    assert adapter is not None
    assert adapter.name == GEMINI_DEFAULT_MODEL


def test_no_key_is_a_supported_state_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§8 requires the batch to complete without Tier 3 when no model is reachable:
    auto-match falls, correctness does not. That makes ``None`` a value the cascade
    carries, not an exception it raises."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    assert build_adapter(provider="gemini") is None
