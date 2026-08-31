"""Metric definitions, scored against generator ground truth.

This is the first code in the project permitted to read `truth_links.csv`, and it is
where every claim the submission makes finally becomes a number rather than a count.

**A match is correct only when its settlement set equals the truth set exactly.**

That definition is the whole point and it is worth stating why the weaker ones were
rejected. On day 5, Tier 0 was matching a batched credit to only its lead settlement —
posting `BNK1 = {STL1}` when the truth was `BNK1 = {STL1, STL2}`. It was 25% of
everything that tier posted. Under link-level scoring the posted pair *is* in truth, so
precision reads 100%. Under subset scoring it also reads 100%. Only exact set equality
calls it what it is: a credit under-explained, a settlement orphaned, and a false match
asserted at confidence 1.0.

A metric that scores that defect as a success is worse than having no metric, because
it launders the failure into evidence.
"""

from __future__ import annotations

from eval.metrics import RunMetrics, compute_metrics

CREDIT_VALUE = {"BNK1": 10_000, "BNK2": 20_000, "BNK3": 30_000, "BNKORPH": 5_000}


def _metrics(
    *,
    posted: dict[str, set[str]],
    truth: dict[str, set[str]],
    exceptions: dict[str, str] | None = None,
    seconds: float = 1.0,
) -> RunMetrics:
    """Score a hand-built run whose right answer is known by construction."""
    return compute_metrics(
        posted={credit: frozenset(members) for credit, members in posted.items()},
        truth={credit: frozenset(members) for credit, members in truth.items()},
        credit_values=CREDIT_VALUE,
        exceptions=exceptions or {},
        seconds=seconds,
    )


# --- the definition -------------------------------------------------------------


def test_an_exactly_correct_batch_scores_as_correct() -> None:
    result = _metrics(
        posted={"BNK1": {"STL1", "STL2"}},
        truth={"BNK1": {"STL1", "STL2"}},
    )

    assert result.matches_correct == 1
    assert result.precision == 1.0
    assert result.false_match_rate == 0.0


def test_a_partially_explained_batch_is_a_false_match() -> None:
    """Day 5's defect, expressed as a measurement.

    Posting one member of a two-member batch under-explains the credit and orphans the
    other settlement. Link-level and subset scoring both call this correct, which is
    exactly why neither is used.
    """
    result = _metrics(
        posted={"BNK1": {"STL1"}},
        truth={"BNK1": {"STL1", "STL2"}},
    )

    assert result.matches_correct == 0
    assert result.false_match_rate == 1.0


def test_an_over_explained_credit_is_a_false_match() -> None:
    """The mirror case: claiming a settlement the credit did not cover spends money
    twice, and no later tier recomputes it."""
    result = _metrics(
        posted={"BNK1": {"STL1", "STL2", "STL3"}},
        truth={"BNK1": {"STL1", "STL2"}},
    )

    assert result.matches_correct == 0
    assert result.false_match_rate == 1.0


def test_matching_an_orphan_credit_is_a_false_match() -> None:
    """An out-of-band transfer has no gateway counterpart. Anything posted against it
    is money attributed to a payment that never happened."""
    result = _metrics(
        posted={"BNKORPH": {"STL9"}},
        truth={"BNKORPH": set()},
    )

    assert result.matches_correct == 0
    assert result.false_match_rate == 1.0


def test_declining_an_orphan_credit_is_not_penalised() -> None:
    """Leaving an unmatchable credit unmatched is the correct outcome, not a miss."""
    result = _metrics(posted={}, truth={"BNKORPH": set()})

    assert result.matches_posted == 0
    assert result.credits_explainable == 0
    assert result.recall == 1.0, "recall over an empty set of explainable credits is 1"


# --- the arithmetic -------------------------------------------------------------


def test_precision_recall_and_false_match_on_a_known_case() -> None:
    """Three credits are explainable. Two are posted, one of them wrongly.

    posted   = 2   correct = 1   explainable = 3
    precision   = 1/2 = 0.50
    recall      = 1/3 = 0.333
    false-match = 1/2 = 0.50
    """
    result = _metrics(
        posted={"BNK1": {"STL1"}, "BNK2": {"STL9"}},
        truth={"BNK1": {"STL1"}, "BNK2": {"STL2"}, "BNK3": {"STL3"}},
    )

    assert result.matches_posted == 2
    assert result.matches_correct == 1
    assert result.credits_explainable == 3
    assert result.precision == 0.5
    assert round(result.recall, 3) == 0.333
    assert result.false_match_rate == 0.5


def test_precision_and_false_match_rate_are_complements() -> None:
    """They are two views of one number, and reporting both is deliberate: precision is
    what a builder quotes, false-match rate is what a finance team asks about."""
    result = _metrics(
        posted={"BNK1": {"STL1"}, "BNK2": {"STL9"}},
        truth={"BNK1": {"STL1"}, "BNK2": {"STL2"}},
    )

    assert result.precision + result.false_match_rate == 1.0


def test_metrics_are_defined_when_nothing_was_posted() -> None:
    """A T0-only arm on a hard fixture can post nothing. Division by zero here would
    take out the whole ablation table."""
    result = _metrics(posted={}, truth={"BNK1": {"STL1"}})

    assert result.matches_posted == 0
    assert result.precision == 1.0, "posting nothing wrong is vacuously precise"
    assert result.false_match_rate == 0.0
    assert result.recall == 0.0


# --- the honest denominator -----------------------------------------------------


def test_auto_match_rate_is_reported_over_every_credit() -> None:
    """§9.1's headline. The denominator includes credits that can never be matched,
    because excluding them would flatter the number."""
    result = _metrics(
        posted={"BNK1": {"STL1"}},
        truth={"BNK1": {"STL1"}, "BNK2": {"STL2"}, "BNKORPH": set()},
    )

    assert result.credits_total == 3
    assert result.credits_explainable == 2
    assert round(result.auto_match_rate, 3) == 0.333


def test_value_reconciled_and_value_at_risk_are_reported_separately() -> None:
    """§9.1: speaks the finance team's language. Rupees posted against rupees still
    sitting in the queue."""
    result = _metrics(
        posted={"BNK1": {"STL1"}},
        truth={"BNK1": {"STL1"}, "BNK2": {"STL2"}},
        exceptions={"BNK2": "NO_CANDIDATE"},
    )

    assert result.value_reconciled_paise == 10_000
    assert result.value_at_risk_paise == 20_000


def test_exception_distribution_is_reported_by_code() -> None:
    """§9.1 calls this the honesty metric. A batch that quarantines forty rows and
    reports 95% on the rest is lying by omission."""
    result = _metrics(
        posted={},
        truth={"BNK1": {"STL1"}, "BNK2": {"STL2"}, "BNK3": {"STL3"}},
        exceptions={
            "BNK1": "AMBIGUOUS_SUBSET",
            "BNK2": "AMBIGUOUS_SUBSET",
            "BNK3": "NO_CANDIDATE",
        },
    )

    assert result.exceptions_by_code == {"AMBIGUOUS_SUBSET": 2, "NO_CANDIDATE": 1}


def test_throughput_is_records_per_second() -> None:
    result = _metrics(posted={}, truth={"BNK1": {"STL1"}}, seconds=2.0)

    assert result.seconds == 2.0
    assert result.credits_total == 1


# --- what is not measurable yet -------------------------------------------------


def test_model_metrics_are_absent_rather_than_zero() -> None:
    """Tier 3 does not exist yet. Reporting a cost of zero would imply we measured it
    and found none; reporting nothing says we have not looked. Rule 7 is about not
    inventing numbers, and a confident zero is an invented number.
    """
    result = _metrics(posted={"BNK1": {"STL1"}}, truth={"BNK1": {"STL1"}})

    assert result.llm_invocations == 0
    assert result.hallucinations == 0
    assert result.cost_paise is None
