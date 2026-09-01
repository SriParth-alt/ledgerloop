"""Tier 4 — the exception queue, and clustering.

Two things happen here, and the first matters more than the headline suggests.

**The sweep.** §6 says every unresolved record lands in the queue with a machine-readable
reason code. Until today that was false: a credit that survived all four tiers unmatched
and unobjected-to simply vanished — counted in `unmatched_bank_txns`, present in no
exception row, invisible to anyone reading the queue. Roughly 56 credits on the
adversarial fixture. A batch that quarantines forty rows and reports 95% on the rest is
lying by omission, and so is one that leaves fifty-six credits unaccounted for.

**The clustering.** Twelve exceptions sharing one reason code is not twelve problems. It
is one wrong assumption, usually in the fee model, and the queue's job is to say so. That
is what turns a to-do list into a diagnostic instrument.

The queue is sorted by rupee value at risk, never by row order. An associate with twenty
minutes should spend them on the four-lakh exception, not the three-hundred-rupee one.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ledgerloop.audit.provenance import ProposedException, record_exception
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.exceptions.clustering import (
    SUGGESTED_ACTION,
    cluster,
    open_exceptions,
)
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.synth import generate_fixture
from ledgerloop.ingest.loader import load_batch
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "queue-run"


@pytest.fixture
def swept(tmp_path: Path):
    paths = generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)
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
        report = reconcile(conn, RUN_ID, tiers=frozenset({0, 1, 2}))
        yield conn, report


def _raise(conn, code: ExceptionCode, *, credit: str, value: int) -> None:
    record_exception(
        conn,
        RUN_ID,
        ProposedException(
            code=code,
            bank_txn_id=credit,
            settlement_id=None,
            value_at_risk_paise=value,
            detail={},
        ),
    )


# =================================================================================
# The sweep — nothing is left unaccounted for
# =================================================================================


def test_every_credit_is_either_matched_or_carries_a_reason_code(swept) -> None:
    """The property that makes the queue trustworthy.

    Before the sweep existed, a credit no tier objected to left no trace at all. A queue
    that only contains records something actively complained about understates the work
    remaining, and the auto-match rate would be quoted against a denominator that had
    quietly lost its remainder.
    """
    conn, _ = swept
    total = conn.execute(text("SELECT COUNT(*) FROM bank_txns")).scalar_one()
    matched = conn.execute(
        text("SELECT COUNT(DISTINCT bank_txn_id) FROM match_records")
    ).scalar_one()
    raised = conn.execute(
        text(
            "SELECT COUNT(DISTINCT bank_txn_id) FROM exceptions "
            "WHERE run_id = :r AND bank_txn_id IS NOT NULL"
        ),
        {"r": RUN_ID},
    ).scalar_one()

    assert matched + raised == total


def test_a_credit_with_no_candidates_at_all_is_an_orphan(swept) -> None:
    """An out-of-band transfer has nothing in its window. The matcher cannot *know* it is
    an orphan — only ground truth knows that — so this is an inference from absence, and
    the reason code says which absence."""
    conn, _ = swept
    codes = {
        row[0]
        for row in conn.execute(
            text("SELECT DISTINCT reason_code FROM exceptions WHERE run_id = :r"),
            {"r": RUN_ID},
        )
    }

    assert ExceptionCode.ORPHAN_CREDIT.value in codes


def test_a_credit_with_candidates_that_did_not_fit_is_no_candidate(swept) -> None:
    conn, _ = swept
    codes = {
        row[0]
        for row in conn.execute(
            text("SELECT DISTINCT reason_code FROM exceptions WHERE run_id = :r"),
            {"r": RUN_ID},
        )
    }

    assert ExceptionCode.NO_CANDIDATE.value in codes


def test_the_sweep_never_overwrites_a_reason_a_tier_already_gave(swept) -> None:
    """A credit Tier 2 declined as AMBIGUOUS_SUBSET must keep that code. Replacing it
    with NO_CANDIDATE would discard the two explanations a human needs to choose between,
    and turn a decidable exception into an opaque one.
    """
    conn, _ = swept
    ambiguous = conn.execute(
        text(
            "SELECT COUNT(*) FROM exceptions WHERE run_id = :r AND reason_code = :c"
        ),
        {"r": RUN_ID, "c": ExceptionCode.AMBIGUOUS_SUBSET.value},
    ).scalar_one()

    assert ambiguous > 0
    duplicated = conn.execute(
        text(
            "SELECT bank_txn_id FROM exceptions WHERE run_id = :r AND bank_txn_id IS NOT NULL "
            "GROUP BY bank_txn_id HAVING COUNT(*) > 1"
        ),
        {"r": RUN_ID},
    ).all()

    assert duplicated == [], "a credit carries exactly one reason, not several"


# =================================================================================
# The queue — sorted by what is at stake
# =================================================================================


def test_the_queue_is_sorted_by_rupee_value_at_risk(swept) -> None:
    """§6's product-sense detail. An associate with twenty minutes should spend them on
    the four-lakh exception, not the three-hundred-rupee one."""
    conn, _ = swept
    items = open_exceptions(conn, RUN_ID)

    assert items
    values = [item.value_at_risk_paise for item in items]
    assert values == sorted(values, reverse=True)


def test_row_order_does_not_influence_the_queue(swept) -> None:
    """Guards the guard: a queue that happened to be value-sorted because the fixture
    was would pass the test above while sorting by nothing at all."""
    conn, _ = swept
    _raise(conn, ExceptionCode.NO_CANDIDATE, credit="BNKLAST", value=99_00_00_000)
    items = open_exceptions(conn, RUN_ID)

    assert items[0].bank_txn_id == "BNKLAST"


def test_resolved_exceptions_leave_the_queue(swept) -> None:
    """The queue is what is still open. A resolved row belongs to the audit trail, not
    to the associate's morning."""
    conn, _ = swept
    before = len(open_exceptions(conn, RUN_ID))
    conn.execute(
        text(
            "UPDATE exceptions SET resolved_at = '2026-09-01', resolved_by = 'analyst' "
            "WHERE exception_id = (SELECT exception_id FROM exceptions LIMIT 1)"
        )
    )

    assert len(open_exceptions(conn, RUN_ID)) == before - 1


def test_every_reason_code_carries_a_suggested_action() -> None:
    """§6: the queue shows a suggested next action. A reason code with no guidance tells
    an associate what happened and not what to do about it."""
    for code in ExceptionCode:
        assert code in SUGGESTED_ACTION, f"{code} has no suggested action"
        assert SUGGESTED_ACTION[code].strip()


# =================================================================================
# Clustering — twelve exceptions, one wrong assumption
# =================================================================================


def test_exceptions_sharing_a_code_collapse_into_one_cluster(swept) -> None:
    conn, _ = swept
    for index in range(12):
        _raise(conn, ExceptionCode.AMOUNT_BEYOND_TOLERANCE, credit=f"BNKC{index}", value=1_000)

    clusters = {item.code: item for item in cluster(open_exceptions(conn, RUN_ID))}
    tolerance = clusters[ExceptionCode.AMOUNT_BEYOND_TOLERANCE]

    assert tolerance.count == 12
    assert tolerance.value_at_risk_paise == 12_000


def test_clusters_are_ordered_by_value_not_by_count(swept) -> None:
    """Twenty three-hundred-rupee exceptions matter less than one four-lakh one. Ordering
    by count would put the noise first."""
    conn, _ = swept
    for index in range(20):
        _raise(conn, ExceptionCode.DATE_OUT_OF_WINDOW, credit=f"BNKD{index}", value=300)
    _raise(conn, ExceptionCode.LOW_CONFIDENCE, credit="BNKBIG", value=4_00_000_00)

    clusters = cluster(open_exceptions(conn, RUN_ID))

    assert clusters[0].code is ExceptionCode.LOW_CONFIDENCE
    values = [item.value_at_risk_paise for item in clusters]
    assert values == sorted(values, reverse=True)


def test_a_cluster_names_the_diagnosis_not_just_the_count(swept) -> None:
    """§8's strongest product argument: twelve exceptions sharing a code and a merchant is
    one wrong assumption, usually in the fee model. The cluster has to say that, or it is
    a group-by rather than a diagnosis.
    """
    conn, _ = swept
    for index in range(12):
        _raise(conn, ExceptionCode.AMOUNT_BEYOND_TOLERANCE, credit=f"BNKE{index}", value=1_000)

    clusters = {item.code: item for item in cluster(open_exceptions(conn, RUN_ID))}
    diagnosis = clusters[ExceptionCode.AMOUNT_BEYOND_TOLERANCE].diagnosis

    assert "fee model" in diagnosis.lower()


def test_a_single_exception_is_not_reported_as_a_pattern(swept) -> None:
    """One row is a row. Calling it a cluster would manufacture a diagnosis from noise."""
    conn, _ = swept
    _raise(conn, ExceptionCode.LOW_CONFIDENCE, credit="BNKONE", value=500)

    clusters = {item.code: item for item in cluster(open_exceptions(conn, RUN_ID))}
    lonely = clusters[ExceptionCode.LOW_CONFIDENCE]

    assert lonely.count == 1
    assert "wrong assumption" not in lonely.diagnosis.lower()
