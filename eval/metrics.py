"""Metric definitions.

Auto-match rate, precision, recall, FALSE-MATCH RATE (the headline), exception
distribution by code, LLM invocation rate, cost per 100 records, throughput,
hallucination count, value reconciled vs value at risk.

False-match rate is the metric that distinguishes this submission. Everyone
reports matches found. Almost nobody reports matches wrongly asserted.

**A match is correct only when its settlement set equals the truth set exactly.**

That definition carries the whole file, and the weaker ones were rejected for a concrete
reason. On day 5, Tier 0 was matching a batched credit to only its lead settlement —
posting ``{STL1}`` where the truth was ``{STL1, STL2}`` — on 25% of everything it posted.
Link-level scoring calls that correct, because the posted pair really is in truth.
Subset scoring calls it correct too. Only exact equality calls it what it is: a credit
under-explained, a settlement orphaned, and certainty asserted at confidence 1.0.

A metric that scores that defect as a success is worse than no metric, because it
launders the failure into evidence.

**Nothing is silently excluded.** ``matches_correct + matches_incorrect == matches_posted``
always. Dropping the matches that are awkward to classify is how a false-match rate
quietly becomes flattering.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunMetrics:
    """Everything §9.1 asks for, scored against ground truth.

    ``cost_paise`` and the model counters are ``None`` or zero only when genuinely
    unmeasured — see ``model_metrics_available``. A confident zero would imply we looked
    and found none.
    """

    credits_total: int
    credits_explainable: int
    matches_posted: int
    matches_correct: int
    matches_incorrect: int

    auto_match_rate: float
    precision: float
    recall: float
    false_match_rate: float

    exceptions_by_code: dict[str, int] = field(default_factory=dict)

    llm_invocations: int = 0
    cache_hits: int = 0
    hallucinations: int = 0
    cost_paise: int | None = None

    value_reconciled_paise: int = 0
    value_at_risk_paise: int = 0

    seconds: float = 0.0

    @property
    def adjudications(self) -> int:
        """How many credits this configuration put to the model.

        The reproducible figure, and the one §9.2 means by "LLM calls". A *new API call*
        count is a property of the cache, not of the configuration: the first published
        adversarial run showed the full cascade making 3 calls where a cold cache would
        have made 67, because a crashed earlier sweep had already paid for 64 of them.
        Quoted as-is that would have understated the cascade's model usage twenty-fold —
        in the flattering direction, which is exactly the sort of number this project has
        to be most suspicious of.
        """
        return self.llm_invocations + self.cache_hits

    @property
    def throughput(self) -> float:
        """Credits per second. §9.1; the track bar asks for it explicitly."""
        return self.credits_total / self.seconds if self.seconds else 0.0


def compute_metrics(
    *,
    posted: dict[str, frozenset[str]],
    truth: dict[str, frozenset[str]],
    credit_values: dict[str, int],
    exceptions: dict[str, str],
    seconds: float,
    llm_invocations: int = 0,
    cache_hits: int = 0,
    hallucinations: int = 0,
    cost_paise: int | None = None,
) -> RunMetrics:
    """Score one run.

    ``posted`` and ``truth`` both map a credit to the set of settlements that explain
    it. An orphan credit appears in ``truth`` with an **empty** set — present and
    explained by nothing — rather than being absent, which would mean "we do not know".
    """
    correct = 0
    incorrect = 0
    reconciled = 0

    for credit, members in posted.items():
        expected = truth.get(credit)
        if expected is not None and members == expected and members:
            correct += 1
            reconciled += credit_values.get(credit, 0)
        else:
            incorrect += 1

    explainable = {credit for credit, members in truth.items() if members}

    at_risk = sum(credit_values.get(credit, 0) for credit in exceptions)

    return RunMetrics(
        credits_total=len(truth),
        credits_explainable=len(explainable),
        matches_posted=len(posted),
        matches_correct=correct,
        matches_incorrect=incorrect,
        # Denominator is every credit, including the ones that can never be matched.
        # Excluding them would flatter the number; `credits_explainable` sits beside it
        # so a reader can see both without being told which to trust.
        auto_match_rate=_ratio(len(posted), len(truth), when_empty=0.0),
        # Posting nothing wrong is vacuously precise. The alternative — zero — would
        # report a T0-only arm that declined everything as maximally wrong.
        precision=_ratio(correct, len(posted), when_empty=1.0),
        recall=_ratio(correct, len(explainable), when_empty=1.0),
        false_match_rate=_ratio(incorrect, len(posted), when_empty=0.0),
        exceptions_by_code=dict(Counter(exceptions.values())),
        llm_invocations=llm_invocations,
        cache_hits=cache_hits,
        hallucinations=hallucinations,
        cost_paise=cost_paise,
        value_reconciled_paise=reconciled,
        value_at_risk_paise=at_risk,
        seconds=seconds,
    )


def _ratio(numerator: int, denominator: int, *, when_empty: float) -> float:
    """Divide, with an explicit answer for the empty case.

    Every one of these denominators can legitimately be zero — a T0-only arm on a hard
    fixture posts nothing, and a fixture of pure orphans has nothing explainable. An
    unguarded division here would take out the whole ablation table.
    """
    return numerator / denominator if denominator else when_empty
