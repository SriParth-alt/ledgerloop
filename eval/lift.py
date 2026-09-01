"""§9.3 — the measured lift from rule promotion.

Run the cascade, resolve the top exceptions, promote what generalises, run again over the
same fixture, and report the delta. This is the evidence the agentic loop does something
rather than being a UI flourish.

**The human is simulated by ground truth, and only here.** Someone has to decide what the
correct resolutions are, and `eval/` is the one package permitted to read
`truth_links.csv`. So it plays a perfect analyst: take the queue's own ordering — highest
rupee value at risk first, exactly what a person would see — look up the settlements that
actually explain each credit, resolve, promote. Hand-picking five resolutions would let
whoever wrote this choose the five that flatter the number.

**The matcher still never sees truth.** It is handed a `RuleStore` containing rules a
human approved, which is the same thing it would receive in production. Nothing about the
second run knows what the answers are.

**Fewer rules than resolutions is the honest outcome.** Not every resolution contains a
generalisable lesson, and reporting five promotions when two were made would overstate
what the loop learned.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from eval.harness import Truth, load_truth, score_run
from eval.metrics import RunMetrics
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.clustering import open_exceptions
from ledgerloop.generate.synth import (
    BANK_FILE,
    INVOICES_FILE,
    SETTLEMENTS_FILE,
    generate_fixture,
)
from ledgerloop.ingest.loader import load_batch
from ledgerloop.rules.promote import (
    EMPTY_STORE,
    RuleStore,
    attach_promoted_rule,
    load_rules,
    promote,
    propose_rule,
    record_resolution,
)
from ledgerloop.store.db import connect, initialise, start_run

DETERMINISTIC_TIERS = frozenset({0, 1, 2})


@dataclass(frozen=True)
class LiftMeasurement:
    """What §9.3 asks for: the same fixture, before and after promotion."""

    before: RunMetrics
    after: RunMetrics
    before_run_id: str
    after_run_id: str
    resolutions_used: int
    rules_promoted: int

    @property
    def auto_match_delta(self) -> float:
        return self.after.auto_match_rate - self.before.auto_match_rate


def measure_lift(
    fixture: str,
    *,
    records: int,
    seed: int,
    workdir: Path,
    resolutions: int = 5,
) -> LiftMeasurement:
    """Measure the auto-match delta from promoting rules learned by resolving exceptions."""
    workdir = Path(workdir)
    paths = generate_fixture(
        fixture=fixture, settlements=records, seed=seed, out_dir=workdir / "fixtures"
    )
    truth = load_truth(paths["truth"])
    source = workdir / "fixtures" / fixture
    store_path = workdir / "store.yaml"

    before, promoted = _run_and_learn(
        workdir, source, fixture, truth, store_path, resolutions=resolutions
    )
    after = _run_with(
        workdir, source, fixture, truth, "after", load_rules(store_path)
    )

    return LiftMeasurement(
        before=before,
        after=after,
        before_run_id="before",
        after_run_id="after",
        resolutions_used=resolutions,
        rules_promoted=promoted,
    )


def _run_and_learn(
    workdir: Path,
    source: Path,
    fixture: str,
    truth: Truth,
    store_path: Path,
    *,
    resolutions: int,
) -> tuple[RunMetrics, int]:
    """Reconcile, then resolve the most valuable exceptions and promote what generalises."""
    promoted = 0
    with connect(workdir / "before.db") as conn:
        metrics = _reconcile_and_score(conn, "before", source, fixture, truth, EMPTY_STORE)

        # The queue's own ordering — largest exposure first, which is what a person
        # opening it sees. They work *down* the list resolving what they can: a batched
        # credit needs several settlements named at once, which this resolution shape
        # does not express, and an orphan has nothing to name at all. Both are skipped
        # rather than forced, and the first `resolutions` that a human could actually
        # settle are the ones used.
        for item in open_exceptions(conn, "before"):
            if promoted >= resolutions:
                break

            settlement_id = _correct_settlement(truth, item.bank_txn_id)
            if settlement_id is None:
                # Ground truth says nothing explains this credit — an orphan, a re-post,
                # or a batch. Inventing a settlement here would teach the system a rule
                # from a resolution no careful human would have made.
                continue

            resolution = record_resolution(
                conn,
                "before",
                item.exception_id,
                settlement_id=settlement_id,
                resolved_by="eval-harness",
            )
            rule = propose_rule(resolution)
            if rule is None:
                continue

            promote(rule, store_path, approved_by="eval-harness")
            attach_promoted_rule(conn, item.exception_id, rule.value)
            promoted += 1

    return metrics, promoted


def _run_with(
    workdir: Path,
    source: Path,
    fixture: str,
    truth: Truth,
    run_id: str,
    rules: RuleStore,
) -> RunMetrics:
    """Reconcile the same fixture again, this time with the approved rules in force.

    A fresh database and a fresh run id. Matches are append-only and idempotency is
    scoped per run (ADR-013), so reusing either would make the second pass a no-op and
    report a delta of zero for a reason unrelated to the rules.
    """
    with connect(workdir / f"{run_id}.db") as conn:
        return _reconcile_and_score(conn, run_id, source, fixture, truth, rules)


def _reconcile_and_score(
    conn,
    run_id: str,
    source: Path,
    fixture: str,
    truth: Truth,
    rules: RuleStore,
) -> RunMetrics:
    initialise(conn)
    start_run(
        conn,
        run_id=run_id,
        fixture=fixture,
        tiers_enabled=",".join(str(tier) for tier in sorted(DETERMINISTIC_TIERS)),
        config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
    )
    load_batch(
        conn,
        run_id,
        invoices=source / INVOICES_FILE,
        settlements=source / SETTLEMENTS_FILE,
        bank_statement=source / BANK_FILE,
    )
    reconcile(conn, run_id, tiers=DETERMINISTIC_TIERS, rules=rules)
    return score_run(conn, run_id, truth, seconds=0.0)


def _correct_settlement(truth: Truth, bank_txn_id: str | None) -> str | None:
    """The settlement a perfect analyst would name for this credit.

    Only 1:1 explanations are used. A batched credit has several correct settlements and
    naming one of them would be recording a resolution a careful human would not make —
    and would hand the promoter a partial explanation to generalise from.
    """
    if bank_txn_id is None:
        return None
    members = truth.explanations.get(bank_txn_id, frozenset())
    if len(members) != 1:
        return None
    return next(iter(members))
