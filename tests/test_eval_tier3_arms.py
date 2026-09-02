"""The two §9.2 rows that need a model, and the rule that governs them.

**All-or-nothing.** 677 calls against a 500-per-day free tier means a sweep will be
interrupted, probably more than once. The tempting behaviour is to score whatever came
back and note the gap. That would put a number in `results/metrics.md` computed over the
fraction of a fixture that happened to fit inside a quota window — a figure nobody could
reproduce and everybody would quote.

So an arm that could not ask about every credit it needed to reports *not yet measured*,
exactly as it does today while unimplemented. The cache keeps the answers already paid
for; tomorrow's attempt resumes and costs nothing for them.

Nothing here calls a model. `ScriptedAdapter` counts invocations, which is the only way
to assert §7.4's real promise: a re-run performs **zero** new calls. That is not
observable from the match set, because a correct answer looks identical whether it was
cached or bought again.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval.ablation import (
    NOT_MEASURED,
    CallEstimate,
    estimate_calls,
    run_ablation,
    write_report,
)
from ledgerloop.llm.adapter import ScriptedAdapter, SweepInterruptedError
from ledgerloop.llm.cache import ResponseCache

FIXTURE = "realistic"
RECORDS = 60
SEED = 42


class _QuotaLimitedAdapter(ScriptedAdapter):
    """Declines every credit, then runs out of quota the way the real one does.

    Subclassed rather than reusing ``ScriptedAdapter`` directly because running dry means
    two different things. For the gate tests it is a *test* bug — the code asked more
    often than the test anticipated — and an ``AssertionError`` should say so loudly.
    Here it is the production condition being simulated, and the eval must see exactly
    the exception a spent free tier raises.
    """

    def complete(self, prompt: str) -> str:
        if not self.responses:
            raise SweepInterruptedError("scripted quota exhausted")
        return super().complete(prompt)


def _declines(count: int) -> _QuotaLimitedAdapter:
    """A model that always declines: schema-valid, and it matches nothing.

    Deliberately not a model that matches things. These tests are about whether the arms
    *run and report honestly*, and a decline exercises that without pinning the metrics
    to a scripted answer that means nothing.
    """
    return _QuotaLimitedAdapter(
        responses=['{"settlement_ids": [], "confidence": 0.0, "reasoning": "no"}']
        * count
    )


# --- estimating before spending ----------------------------------------------------


def test_the_estimate_sends_nothing(tmp_path: Path) -> None:
    """The whole point of `--estimate-only`: the user approves a call count before a
    single request is paid for."""
    adapter = ScriptedAdapter(responses=[])  # raises if called at all

    estimate = estimate_calls(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path, adapter=adapter
    )

    assert isinstance(estimate, CallEstimate)
    assert adapter.calls == 0


def test_the_estimate_counts_both_model_arms_separately(tmp_path: Path) -> None:
    """They cost wildly different amounts — the full cascade only asks about the
    residual, the baseline asks about every credit — and a single total would hide
    which one to cut when the quota is short.

    Pinned to `adversarial` because `realistic` at this size has **no residual at all**:
    T0-T2 resolve every credit, so the full cascade would make zero calls and the
    separation this test is about would not be visible. That zero is a real result, not
    a defect — it is the cascade working.
    """
    estimate = estimate_calls(
        "adversarial", records=RECORDS, seed=SEED, workdir=tmp_path
    )

    assert estimate.full_cascade_calls > 0
    assert estimate.llm_only_calls > estimate.full_cascade_calls
    assert estimate.total_calls == estimate.full_cascade_calls + estimate.llm_only_calls


# --- all-or-nothing scoring --------------------------------------------------------


def test_a_complete_sweep_produces_real_numbers(tmp_path: Path) -> None:
    arms = run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(500),
        cache=ResponseCache(tmp_path / "cache"),
    )

    by_label = {arm.label: arm for arm in arms}
    assert by_label["Full cascade"].metrics is not None
    assert by_label["LLM-only baseline"].metrics is not None


def test_an_interrupted_sweep_reports_no_number_at_all(tmp_path: Path) -> None:
    """The rule this file exists for.

    A quota that runs out mid-fixture must not yield a match rate over the part that
    fit. Rule 7 is as much about not *implying* a number as about not typing one.
    """
    arms = run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(3),  # runs dry almost immediately
        cache=ResponseCache(tmp_path / "cache"),
    )

    by_label = {arm.label: arm for arm in arms}
    assert by_label["LLM-only baseline"].metrics is None
    assert by_label["LLM-only baseline"].note == NOT_MEASURED


def test_an_interrupted_sweep_still_leaves_the_cache_warm(tmp_path: Path) -> None:
    """Otherwise every interruption would cost the whole day's quota again, and 677
    calls against 500 per day would never finish."""
    cache_dir = tmp_path / "cache"
    run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(3),
        cache=ResponseCache(cache_dir),
    )

    assert list(cache_dir.glob("*.json")), "answers already paid for were discarded"


def test_the_deterministic_arms_survive_a_model_outage(tmp_path: Path) -> None:
    """§8: the batch completes without Tier 3. T0-T2 are arithmetic and owe the model
    nothing, so an exhausted quota must not take their numbers down with it."""
    arms = run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(0),
        cache=ResponseCache(tmp_path / "cache"),
    )

    for label in ("T0 only", "T0 + T1", "T0 + T1 + T2"):
        arm = next(a for a in arms if a.label == label)
        assert arm.metrics is not None


# --- the cache is the reproducibility guarantee ------------------------------------


def test_a_second_sweep_buys_nothing_it_already_owns(tmp_path: Path) -> None:
    """§7.4's promise, and it is about *calls*, not about answers.

    Two runs producing the same match set proves nothing — a re-purchased answer looks
    identical. Only the invocation count distinguishes a cache that works from one that
    silently misses on every prompt.
    """
    cache = ResponseCache(tmp_path / "cache")
    first = _declines(500)
    run_ablation(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path / "a",
        adapter=first, cache=cache,
    )
    assert first.calls > 0

    second = ScriptedAdapter(responses=[])  # raises the moment it is asked for anything
    run_ablation(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path / "b",
        adapter=second, cache=cache,
    )

    assert second.calls == 0


def test_the_report_stops_claiming_tier_three_does_not_exist(tmp_path: Path) -> None:
    """The trailer currently reads 'because Tier 3 does not exist yet'. Once measured,
    leaving that sentence in place would be a hand-written falsehood sitting directly
    under a generated table."""
    arms = run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(500),
        cache=ResponseCache(tmp_path / "cache"),
    )
    out = tmp_path / "metrics.md"
    write_report({FIXTURE: arms}, out)

    assert "does not exist yet" not in out.read_text(encoding="utf-8")


@pytest.mark.parametrize("label", ["Full cascade", "LLM-only baseline"])
def test_both_model_arms_are_present_whether_or_not_they_ran(
    label: str, tmp_path: Path
) -> None:
    """Present from the start so their absence is visible rather than silent — the same
    reason they were hardcoded as pending before there was anything to run."""
    arms = run_ablation(
        FIXTURE,
        records=RECORDS,
        seed=SEED,
        workdir=tmp_path,
        adapter=_declines(0),
        cache=ResponseCache(tmp_path / "cache"),
    )

    assert any(arm.label == label for arm in arms)


# --- the calls column ---------------------------------------------------------------


def test_adjudications_are_reported_separately_from_new_api_calls(tmp_path: Path) -> None:
    """The defect this test exists to prevent, caught in a published table.

    The first real adversarial run reported the full cascade making **3** model calls.
    A cold cache would have made 67; a crashed earlier sweep had already paid for 64.
    Since §9.2 uses model-call count as a headline comparison against the LLM-only
    baseline, that figure understated the cascade's usage twenty-fold — in the flattering
    direction.

    So the reproducible number is how many credits were *put to* the model. What a given
    run happened to pay for is real, useful, and reported separately.
    """
    cache = ResponseCache(tmp_path / "cache")
    first = run_ablation(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path / "a",
        adapter=_declines(500), cache=cache,
    )
    second = run_ablation(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path / "b",
        adapter=ScriptedAdapter(responses=[]), cache=cache,
    )

    cold = next(a for a in first if a.label == "LLM-only baseline").metrics
    warm = next(a for a in second if a.label == "LLM-only baseline").metrics
    assert cold is not None and warm is not None

    # The configuration asked the same questions both times.
    assert warm.adjudications == cold.adjudications > 0
    # But the second run bought nothing.
    assert cold.llm_invocations > 0
    assert warm.llm_invocations == 0
    assert warm.cache_hits == warm.adjudications


def test_the_report_labels_which_call_count_is_which(tmp_path: Path) -> None:
    """A reader quoting the wrong column is the failure mode; the table has to say
    plainly that one number is architectural and the other is bookkeeping."""
    arms = run_ablation(
        FIXTURE, records=RECORDS, seed=SEED, workdir=tmp_path,
        adapter=_declines(500), cache=ResponseCache(tmp_path / "cache"),
    )
    out = tmp_path / "metrics.md"
    write_report({FIXTURE: arms}, out)
    text = out.read_text(encoding="utf-8")

    assert "Adjudications" in text
    assert "New API calls" in text
