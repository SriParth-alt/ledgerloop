"""The exception lifecycle: how an exception ends, not only how it starts.

Every earlier queue test ran Tiers 0-2, where a credit collects at most one reason. The
full cascade breaks that. Tier 2 declines a credit as POOL_TOO_LARGE, the credit falls
through because only ambiguity is terminal (ADR-020), and Tier 3 matches it — leaving the
Tier 2 exception open on a credit that is reconciled.

On the demo fixture that was 14 matched credits still counted as at risk (Rs 5.69 lakh),
and a queue that summed 108 rows over 67 credits. The published report showed three
different answers to "how much is unresolved" on one page.

Found on 14 Sep 2026 by querying the demo database while preparing to present it, not by
a test (ADR-042). These tests run the real full cascade, keyless, from the committed
cache — the configuration a judge runs — because a queue of hand-built rows is exactly
where this defect could not be seen.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from eval.harness import load_truth, score_run
from ledgerloop.audit.provenance import ProposedException, record_exception
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.clustering import open_exceptions
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.synth import (
    BANK_FILE,
    INVOICES_FILE,
    SETTLEMENTS_FILE,
    TRUTH_FILE,
    generate_fixture,
)
from ledgerloop.ingest.loader import load_batch
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.store.db import connect, initialise, start_run

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_CACHE = REPO_ROOT / "fixtures" / "llm_cache"
RUN = "lifecycle"

#: A match must never silence these. A duplicate warning says the money may be counted
#: twice, and ambiguity is a decision reserved for a human.
NEVER_SUPERSEDED = {
    ExceptionCode.DUPLICATE_SUSPECTED.value,
    ExceptionCode.AMBIGUOUS_SUBSET.value,
}


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """The demo configuration: adversarial, 250 records, seed 42, all four tiers, no key."""
    root = tmp_path_factory.mktemp("lifecycle")
    generate_fixture(fixture="adversarial", settlements=250, seed=42, out_dir=root / "fx")
    source = root / "fx" / "adversarial"
    db = root / "demo.db"
    with connect(db) as conn:
        initialise(conn)
        start_run(
            conn,
            run_id=RUN,
            fixture="adversarial",
            tiers_enabled="0,1,2,3",
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )
        load_batch(
            conn,
            RUN,
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        reconcile(
            conn,
            RUN,
            tiers=frozenset({0, 1, 2, 3}),
            adapter=None,
            cache=ResponseCache(COMMITTED_CACHE),
        )
    return db, source


@pytest.fixture
def conn(demo: tuple[Path, Path]):
    db, _ = demo
    with connect(db) as connection:
        yield connection


def _matched(conn) -> set[str]:
    return {
        row[0]
        for row in conn.execute(
            text(
                "SELECT bank_txn_id FROM match_records "
                "WHERE run_id = :r AND superseded_by IS NULL"
            ),
            {"r": RUN},
        )
    }


# --- guard the guard ----------------------------------------------------------------


def test_the_fixture_really_has_credits_two_tiers_touched(conn) -> None:
    """Without this, every test below could pass over nothing — ADR-037's lesson.

    A credit that carries an exception row (open or closed) *and* a match is the case this
    file exists for. If a fixture change ever removes it, these tests must say so rather
    than go quietly green.
    """
    touched = {
        row[0]
        for row in conn.execute(
            text("SELECT bank_txn_id FROM exceptions WHERE run_id = :r"), {"r": RUN}
        )
    }

    assert touched & _matched(conn), "no matched credit ever carried an exception"


# --- fix 1: a match supersedes the reasons it answered --------------------------------


def test_no_matched_credit_is_left_with_an_open_exception(conn) -> None:
    """The defect itself: Tier 3 matched it, and Tier 2's POOL_TOO_LARGE stayed open."""
    matched = _matched(conn)
    stale = [
        (row.bank_txn_id, row.reason_code)
        for row in conn.execute(
            text(
                "SELECT bank_txn_id, reason_code FROM exceptions "
                "WHERE run_id = :r AND resolved_at IS NULL AND bank_txn_id IS NOT NULL"
            ),
            {"r": RUN},
        )
        if row.bank_txn_id in matched and row.reason_code not in NEVER_SUPERSEDED
    ]

    assert stale == []


def test_a_superseded_exception_names_the_match_that_closed_it(conn) -> None:
    """Closing is not deleting. The trail must say which match answered the exception, so
    "why is this no longer open?" has an answer in the data rather than in someone's head.
    """
    rows = conn.execute(
        text(
            "SELECT bank_txn_id, resolution_json FROM exceptions "
            "WHERE run_id = :r AND resolved_by = 'cascade'"
        ),
        {"r": RUN},
    ).all()

    assert rows, "no exception was superseded by a later tier's match"
    for row in rows:
        detail = json.loads(row.resolution_json)
        match = conn.execute(
            text("SELECT bank_txn_id, tier FROM match_records WHERE match_id = :m"),
            {"m": detail["superseded_by_match"]},
        ).one()
        assert match.bank_txn_id == row.bank_txn_id
        assert detail["tier"] == match.tier


def test_a_match_never_closes_a_duplicate_warning_or_an_ambiguity(tmp_path: Path) -> None:
    """Only "this tier could not match it" is answered by a match. A duplicate warning
    means the money may be counted twice — matching the re-post makes that *more* urgent,
    not less — and ambiguity belongs to a human by policy."""
    from ledgerloop.audit.provenance import supersede_exceptions

    with connect(tmp_path / "unit.db") as c:
        initialise(c)
        start_run(c, run_id=RUN, fixture="unit", tiers_enabled="", config_json="{}")
        for code in (
            ExceptionCode.POOL_TOO_LARGE,
            ExceptionCode.DUPLICATE_SUSPECTED,
            ExceptionCode.AMBIGUOUS_SUBSET,
        ):
            record_exception(
                c,
                RUN,
                ProposedException(
                    code=code,
                    bank_txn_id="BNK1",
                    settlement_id=None,
                    value_at_risk_paise=100,
                    detail={},
                ),
            )

        closed = supersede_exceptions(c, RUN, "BNK1", match_id="M1", tier=3)
        still_open = {
            row[0]
            for row in c.execute(
                text("SELECT reason_code FROM exceptions WHERE resolved_at IS NULL")
            )
        }

    assert closed == 1
    assert still_open == NEVER_SUPERSEDED


def test_supersedable_codes_exclude_what_a_match_must_not_silence() -> None:
    from ledgerloop.exceptions.codes import SUPERSEDABLE, TERMINAL

    assert ExceptionCode.POOL_TOO_LARGE in SUPERSEDABLE
    assert ExceptionCode.DUPLICATE_SUSPECTED not in SUPERSEDABLE
    assert not TERMINAL & SUPERSEDABLE


def test_scored_exceptions_cover_exactly_the_unmatched_credits(demo, conn) -> None:
    """The published figures. Every unmatched credit carries one reason, no matched
    credit carries one, and value at risk is the money that genuinely did not reconcile."""
    _, source = demo
    metrics = score_run(conn, RUN, load_truth(source / TRUTH_FILE), seconds=0.0)
    unmatched_value = conn.execute(
        text(
            "SELECT COALESCE(SUM(credit_paise), 0) FROM bank_txns WHERE run_id = :r "
            "AND bank_txn_id NOT IN (SELECT bank_txn_id FROM match_records "
            "WHERE run_id = :r AND superseded_by IS NULL)"
        ),
        {"r": RUN},
    ).scalar_one()

    assert sum(metrics.exceptions_by_code.values()) == (
        metrics.credits_total - metrics.matches_posted
    )
    assert metrics.value_at_risk_paise == unmatched_value


# --- fix 2: the queue is per credit, and agrees with the scored figures ---------------


def test_the_queue_holds_one_item_per_credit(conn) -> None:
    """An associate works credits, not rows. Two tiers declining the same credit is one
    thing to look at, and its money is at risk once."""
    credits = [item.bank_txn_id for item in open_exceptions(conn, RUN) if item.bank_txn_id]

    assert len(credits) == len(set(credits))


def test_the_queue_and_the_scored_metrics_agree(demo, conn) -> None:
    """Two sources for one number is how a wrong number ships (ADR-036). The CLI queue and
    the HTML report are built from `open_exceptions`; the headline figures from
    `score_run`. They must tell the same story, code by code and rupee for rupee."""
    _, source = demo
    metrics = score_run(conn, RUN, load_truth(source / TRUTH_FILE), seconds=0.0)
    items = [item for item in open_exceptions(conn, RUN) if item.bank_txn_id]

    assert Counter(item.code.value for item in items) == Counter(metrics.exceptions_by_code)
    assert sum(item.value_at_risk_paise for item in items) == metrics.value_at_risk_paise


def test_the_exceptions_command_prints_the_id_resolve_needs(demo) -> None:
    """`resolve --exception-id` says "from `ledgerloop exceptions`". That command printed
    bank ids only, so the agentic loop's front door could not be opened without querying
    SQLite by hand."""
    from ledgerloop.cli import app

    db, _ = demo
    result = CliRunner().invoke(
        app, ["exceptions", "--run-id", RUN, "--db", str(db), "--limit", "3"]
    )
    with connect(db) as c:
        top = open_exceptions(c, RUN)[0]

    assert result.exit_code == 0, result.output
    assert top.exception_id in result.output
