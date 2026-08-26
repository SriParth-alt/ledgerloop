"""The cascade orchestrator, running against a real ingested batch.

Two things are being checked here that the pure Tier 0 tests cannot see: that the
tier actually reaches the rows sitting in SQLite, and that the run-level bookkeeping
the ablation depends on is honest.

The `degraded` distinction matters more than it looks. `--tiers 0,1,2` is a
*deliberate configuration* — an ablation arm, fully valid, comparable to other arms.
`--no-llm` while tier 3 was asked for is a *failure the run survived*. Marking both
degraded would make §9.2's table unreadable; marking neither would let a degraded
run's numbers be compared against a full one's.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ledgerloop.cascade.orchestrator import parse_tiers, reconcile
from ledgerloop.cascade.tier0_exact import RULE_AMOUNT_DATE_UNIQUE, RULE_UTR_EXACT
from ledgerloop.cascade.tier1_tolerant import RULE_TOLERANT
from ledgerloop.generate.synth import generate_fixture
from ledgerloop.ingest.loader import load_batch
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "orch-run"


@pytest.fixture
def ingested(tmp_path: Path):
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
    with connect(tmp_path / "ll.db") as conn:
        initialise(conn)
        start_run(
            conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="0", config_json="{}"
        )
        load_batch(
            conn,
            RUN_ID,
            invoices=paths["invoices"],
            settlements=paths["settlements"],
            bank_statement=paths["bank_statement"],
        )
        yield conn


# --- tier selection -------------------------------------------------------------


def test_parse_tiers_reads_the_ablation_spec() -> None:
    assert parse_tiers("0,1,2,3") == frozenset({0, 1, 2, 3})
    assert parse_tiers("0,1") == frozenset({0, 1})
    assert parse_tiers(" 0 , 2 ") == frozenset({0, 2})


def test_parse_tiers_rejects_nonsense() -> None:
    """A typo in an ablation spec must not silently run a different configuration than
    the one the metrics will be labelled with."""
    with pytest.raises(ValueError):
        parse_tiers("0,x")
    with pytest.raises(ValueError):
        parse_tiers("0,9")


# --- running --------------------------------------------------------------------


def test_reconcile_posts_tier0_matches(ingested) -> None:
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0}))
    posted = conn.execute(text("SELECT COUNT(*) FROM match_records")).scalar_one()

    assert posted > 0, "tier 0 matched nothing on the adversarial fixture"
    assert report.tiers[0].tier == 0
    assert report.tiers[0].matched_bank_txns == posted


def test_every_posted_match_is_tier_zero_and_certain(ingested) -> None:
    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0}))
    rows = conn.execute(text("SELECT tier, confidence, rule_id FROM match_records")).all()

    assert rows
    for row in rows:
        assert row.tier == 0
        assert row.confidence == 1.0
        assert row.rule_id in {RULE_UTR_EXACT, RULE_AMOUNT_DATE_UNIQUE}


def test_no_settlement_is_matched_by_two_credits(ingested) -> None:
    """Double-counting is the failure that survives every later tier untouched — once
    a settlement is spent twice, nothing downstream recomputes it."""
    import json

    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0}))
    rows = conn.execute(text("SELECT settlement_ids_json FROM match_records")).all()
    claimed = [sid for row in rows for sid in json.loads(row.settlement_ids_json)]

    assert len(claimed) == len(set(claimed))


def test_reposted_credit_is_never_matched_by_tier0(ingested) -> None:
    """BNKDUP001 carries the same money, date and narration as a real credit. Its
    reference is therefore not unique on the bank side, and its amount-and-date key is
    shared — so both Tier 0 rules must decline it."""
    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0}))
    matched = conn.execute(
        text("SELECT COUNT(*) FROM match_records WHERE bank_txn_id = 'BNKDUP001'")
    ).scalar_one()

    assert matched == 0


def test_rerunning_the_cascade_does_not_double_post(ingested) -> None:
    """Match records are append-only, so a second pass that re-posted the same matches
    would silently double every count the report is built from."""
    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0}))
    first = conn.execute(text("SELECT COUNT(*) FROM match_records")).scalar_one()

    reconcile(conn, RUN_ID, tiers=frozenset({0}))
    second = conn.execute(text("SELECT COUNT(*) FROM match_records")).scalar_one()

    assert second == first


# --- run bookkeeping ------------------------------------------------------------


def test_an_ablation_arm_is_not_marked_degraded(ingested) -> None:
    """Running fewer tiers on purpose is a configuration, not a failure."""
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}))
    stored = conn.execute(
        text("SELECT degraded FROM runs WHERE run_id = :r"), {"r": RUN_ID}
    ).scalar_one()

    assert report.degraded is False
    assert stored == 0


def test_no_llm_with_tier3_requested_marks_the_run_degraded(ingested) -> None:
    """§8: the batch completes without Tier 3, auto-match rate falls, correctness does
    not — and the run says so, because its numbers are not comparable to a full run's."""
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2, 3}), no_llm=True)
    stored = conn.execute(
        text("SELECT degraded FROM runs WHERE run_id = :r"), {"r": RUN_ID}
    ).scalar_one()

    assert report.degraded is True
    assert stored == 1


def test_unimplemented_tiers_report_zero_rather_than_being_skipped(ingested) -> None:
    """A requested tier that contributed nothing must still appear in the report. A
    missing row reads as 'not asked for'; a zero reads as 'asked for, found nothing',
    and only the second is true today."""
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}))

    assert [outcome.tier for outcome in report.tiers] == [0, 1, 2]
    assert report.tiers[2].matched_bank_txns == 0


def test_report_counts_what_is_left_unmatched(ingested) -> None:
    """The residual is the number Tier 1 inherits, and the exception queue's eventual
    size. Reporting only successes is how a 95% match rate gets quoted on a shrunken
    denominator."""
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0}))
    total_bank = conn.execute(text("SELECT COUNT(*) FROM bank_txns")).scalar_one()

    assert report.unmatched_bank_txns == total_bank - report.tiers[0].matched_bank_txns
    assert report.unmatched_settlements > 0


def test_tier_progress_is_reported_as_it_goes(ingested) -> None:
    """The streaming per-tier count is the moment the architecture becomes visible in
    the demo. The orchestrator hands it to a callback rather than printing, so it stays
    testable and the library stays quiet."""
    conn = ingested
    seen: list[int] = []

    reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}), on_tier=lambda o: seen.append(o.tier))

    assert seen == [0, 1, 2]


def test_tier1_resolves_credits_tier0_declined(ingested) -> None:
    """The tiers must compose: Tier 1 inherits Tier 0's residual and adds to it.

    If this ever showed zero, the most likely cause is not that Tier 1 is weak but
    that the orchestrator handed it rows Tier 0 had already claimed, or none at all.
    """
    conn = ingested
    report = reconcile(conn, RUN_ID, tiers=frozenset({0, 1}))

    assert report.tiers[1].tier == 1
    assert report.tiers[1].matched_bank_txns > 0


def test_tier1_never_reclaims_a_settlement_tier0_posted(ingested) -> None:
    """A settlement spent twice is money counted twice, and no later tier recomputes
    it. This is the failure the orchestrator's shared claim-set exists to prevent."""
    import json

    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0, 1}))
    rows = conn.execute(text("SELECT settlement_ids_json FROM match_records")).all()
    claimed = [sid for row in rows for sid in json.loads(row.settlement_ids_json)]

    assert len(claimed) == len(set(claimed))


def test_tier1_matches_are_recorded_below_full_confidence(ingested) -> None:
    """Confidence 1.0 means "unimpeachable" and belongs to Tier 0 alone. A tolerant
    match that claimed certainty would make the audit trail lie about how it was made.
    """
    conn = ingested
    reconcile(conn, RUN_ID, tiers=frozenset({0, 1}))
    rows = conn.execute(
        text("SELECT confidence, rule_id FROM match_records WHERE tier = 1")
    ).all()

    assert rows
    for row in rows:
        assert row.rule_id == RULE_TOLERANT
        assert 0.90 <= row.confidence < 1.0
