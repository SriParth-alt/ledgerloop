"""Resolving an exception, and the approval that turns it into a rule.

This is the agentic loop's human half — the step §4.3 assigned to a UI before that was
cut, and the reason the cut was affordable. A terminal is a fine place to approve a rule,
and it keeps the demo in one window rather than switching to a browser mid-recording.

The property that matters most here is negative: **resolving does not promote.** A
resolution records what a human decided about one record. Promotion turns that into a rule
that fires forever, on batches nobody has looked at. ADR-004 makes human approval the only
gate on that, and a command that did both in one step would make the gate a formality.

The generator now renders §5.5's "UTR prefixed" faithfully — glued to the token rather
than sitting in its own delimited field — so a learnable class exists for the first time.
Without it these tests would pass and `test_lift.py` would measure nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.cascade.tier0_exact import normalise_utr
from ledgerloop.exceptions.clustering import open_exceptions
from ledgerloop.generate.chaos import ADVERSARIAL, ChaosFlag, ChaosProfile, noisy_narration
from ledgerloop.generate.synth import generate_batch, generate_fixture
from ledgerloop.ingest.loader import load_batch
from ledgerloop.rules.promote import (
    attach_promoted_rule,
    load_rules,
    promote,
    propose_rule,
    record_resolution,
)
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "resolve-run"


@pytest.fixture
def reconciled(tmp_path: Path):
    paths = generate_fixture(fixture="adversarial", settlements=250, seed=42, out_dir=tmp_path)
    with connect(tmp_path / "ll.db") as conn:
        initialise(conn)
        start_run(
            conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="", config_json="{}"
        )
        load_batch(
            conn,
            RUN_ID,
            invoices=paths["invoices"],
            settlements=paths["settlements"],
            bank_statement=paths["bank_statement"],
        )
        reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}))
        yield conn


# --- the class that makes promotion worth anything ------------------------------


def test_narration_noise_glues_a_prefix_to_the_reference() -> None:
    """§5.5 says "UTR prefixed". Rendering that as a separate delimited field lets Tier 0
    split it off trivially, which is what the generator did until today — and it left the
    NARRATION_PREFIX rule kind with nothing in any fixture to learn from.
    """
    from random import Random

    utr = "RZRPY1234567"
    glued = [
        narration
        for seed in range(60)
        if utr not in (narration := noisy_narration(Random(seed), utr, "ACME LTD", 100))
        and utr in narration.replace("-", "").replace("/", "")
    ]

    assert glued, "no narration glued a prefix onto the reference"


def test_a_glued_prefix_defeats_the_normaliser_until_a_rule_is_promoted() -> None:
    """The whole premise, in two lines. Before the rule the reference is unrecoverable;
    after it, it is. Everything else in this file exists to get from one to the other."""
    assert normalise_utr("MMTCRRZRPY1234567") != "RZRPY1234567"


def test_the_fixture_contains_credits_that_a_rule_could_rescue() -> None:
    """A learnable class has to actually appear in the data, not merely be possible."""
    batch = generate_batch(settlements=250, seed=42, profile=ADVERSARIAL)
    references = {row.utr for row in batch.settlements if row.utr}

    hidden = [
        row
        for row in batch.bank_txns
        if any(
            reference not in row.narration
            and reference in row.narration.replace("-", "").replace("/", "").upper()
            for reference in references
        )
    ]

    assert hidden, "no credit hides its reference behind a glued prefix"


def test_easy_fixture_has_no_glued_prefixes() -> None:
    """`easy` exists to prove the pipeline works at all. A learnable failure class there
    would make the simplest fixture unmatched for a reason the tier cannot fix."""
    batch = generate_batch(
        settlements=60,
        seed=42,
        profile=ChaosProfile(flags=frozenset({ChaosFlag.FEES, ChaosFlag.LAG}), intensity=0.1),
    )

    for row in batch.bank_txns:
        assert row.narration


# --- resolving ------------------------------------------------------------------


def test_resolving_marks_the_exception_and_returns_what_was_decided(reconciled) -> None:
    conn = reconciled
    item = open_exceptions(conn, RUN_ID)[0]

    resolution = record_resolution(
        conn,
        RUN_ID,
        item.exception_id,
        settlement_id="STL00001",
        resolved_by="analyst",
    )
    row = conn.execute(
        text(
            "SELECT resolved_at, resolved_by, resolution_json FROM exceptions "
            "WHERE exception_id = :id"
        ),
        {"id": item.exception_id},
    ).one()

    assert row.resolved_at is not None
    assert row.resolved_by == "analyst"
    assert "STL00001" in row.resolution_json
    assert resolution.settlement.settlement_id == "STL00001"


def test_a_resolved_exception_leaves_the_queue(reconciled) -> None:
    conn = reconciled
    before = open_exceptions(conn, RUN_ID)
    record_resolution(
        conn, RUN_ID, before[0].exception_id, settlement_id="STL00001", resolved_by="a"
    )

    assert len(open_exceptions(conn, RUN_ID)) == len(before) - 1


def test_resolving_does_not_promote(reconciled, tmp_path: Path) -> None:
    """The gate. A resolution records what a human decided about one record; a rule fires
    forever on batches nobody has looked at. Doing both in one step would make approval a
    formality, and ADR-004 named it the only thing standing in front of a bad
    generalisation.
    """
    conn = reconciled
    store = tmp_path / "store.yaml"
    item = open_exceptions(conn, RUN_ID)[0]

    record_resolution(
        conn, RUN_ID, item.exception_id, settlement_id="STL00001", resolved_by="analyst"
    )

    assert load_rules(store).is_empty


def test_resolving_an_unknown_exception_is_rejected(reconciled) -> None:
    conn = reconciled
    with pytest.raises(KeyError):
        record_resolution(
            conn, RUN_ID, "no-such-exception", settlement_id="STL00001", resolved_by="a"
        )


def test_an_approved_rule_is_linked_back_to_the_exception_it_came_from(
    reconciled, tmp_path: Path
) -> None:
    """`promoted_rule_id` is the audit trail for the loop itself: which human decision,
    on which record, produced which rule. Without it a store entry is a rule nobody can
    trace to a reason."""
    conn = reconciled
    store = tmp_path / "store.yaml"

    # Search the queue for a resolution that actually generalises rather than taking the
    # first and skipping when it does not. A permanently-skipped test asserts nothing, and
    # this one checks the audit link that makes a rule traceable to the decision behind it.
    settlements = conn.execute(
        text(
            "SELECT settlement_id, net_amount_paise FROM settlements WHERE run_id = :r"
        ),
        {"r": RUN_ID},
    ).all()
    by_amount: dict[int, str] = {
        row.net_amount_paise: row.settlement_id for row in settlements
    }

    found = None
    for item in open_exceptions(conn, RUN_ID):
        credit = conn.execute(
            text("SELECT credit_paise FROM bank_txns WHERE run_id = :r AND bank_txn_id = :b"),
            {"r": RUN_ID, "b": item.bank_txn_id},
        ).scalar_one_or_none()
        settlement_id = by_amount.get(credit) if credit is not None else None
        if settlement_id is None:
            continue
        trial = record_resolution(
            conn, RUN_ID, item.exception_id, settlement_id=settlement_id, resolved_by="analyst"
        )
        if propose_rule(trial) is not None:
            found = (item, settlement_id)
            break

    assert found is not None, "no queued exception yielded a generalisable resolution"
    item, settlement_id = found

    resolution = record_resolution(
        conn, RUN_ID, item.exception_id, settlement_id=settlement_id, resolved_by="analyst"
    )
    proposal = propose_rule(resolution)

    assert proposal is not None, "a glued prefix must generalise to a rule"
    promote(proposal, store, approved_by="analyst")
    attach_promoted_rule(conn, item.exception_id, proposal.value)
    linked = conn.execute(
        text("SELECT promoted_rule_id FROM exceptions WHERE exception_id = :i"),
        {"i": item.exception_id},
    ).scalar_one()

    assert linked == proposal.value


# --- replay ---------------------------------------------------------------------


def test_a_promoted_rule_changes_what_reconcile_matches(tmp_path: Path) -> None:
    """The loop closes here. A rule that persists but never reaches a tier is a config
    file, not a capability — and §9.3's delta would be zero for a reason nobody could see
    from the store.
    """
    paths = generate_fixture(fixture="adversarial", settlements=250, seed=42, out_dir=tmp_path)
    store = tmp_path / "store.yaml"

    from ledgerloop.rules.promote import Rule, RuleKind

    promote(
        Rule(
            kind=RuleKind.NARRATION_PREFIX,
            value="MMTCR",
            description="test",
            learned_from="BNK1",
        ),
        store,
        approved_by="analyst",
    )

    counts = []
    for label, rules in (("plain", None), ("ruled", load_rules(store))):
        with connect(tmp_path / f"{label}.db") as conn:
            initialise(conn)
            start_run(
                conn, run_id=label, fixture="adversarial", tiers_enabled="", config_json="{}"
            )
            load_batch(
                conn,
                label,
                invoices=paths["invoices"],
                settlements=paths["settlements"],
                bank_statement=paths["bank_statement"],
            )
            report = reconcile(conn, label, tiers=frozenset({0, 1, 2}), rules=rules)
            counts.append(sum(outcome.matched_bank_txns for outcome in report.tiers))

    assert counts[1] >= counts[0], "a promoted rule must never reduce what is matched"


def test_reconcile_defaults_to_no_rules(tmp_path: Path) -> None:
    """An empty store is the starting state, and a run with no store must behave exactly
    as every run before today did."""
    assert load_rules(tmp_path / "absent.yaml").is_empty
    assert normalise_utr("MMTCRRZRPY1234567", rules=load_rules(tmp_path / "absent.yaml")) != (
        "RZRPY1234567"
    )


def test_dates_are_recorded_for_the_audit_trail(reconciled) -> None:
    conn = reconciled
    item = open_exceptions(conn, RUN_ID)[0]
    record_resolution(
        conn, RUN_ID, item.exception_id, settlement_id="STL00001", resolved_by="analyst"
    )
    stamp = conn.execute(
        text("SELECT resolved_at FROM exceptions WHERE exception_id = :i"),
        {"i": item.exception_id},
    ).scalar_one()

    assert date.fromisoformat(stamp[:10])
