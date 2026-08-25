"""Store setup: schema, pragmas, and runs as first-class objects.

Nothing currently tests `schema.sql` at all — a syntax error in it would surface as a
confusing failure somewhere downstream rather than here. These tests apply the real
file, so the schema is exercised on every run.

The pragmas matter for correctness, not performance. `foreign_keys = ON` is what stops
a row being written against a run that does not exist, and SQLite has it **off** by
default — a detail that costs people entire afternoons.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.store.db import connect, finish_run, initialise, run_exists, start_run

EXPECTED_TABLES = {
    "runs",
    "invoices",
    "settlements",
    "bank_txns",
    "quarantine",
    "match_records",
    "exceptions",
    "rules",
}

EXPECTED_FINGERPRINT_INDEXES = {
    "idx_invoices_sha",
    "idx_settlements_sha",
    "idx_bank_txns_sha",
}


def _config_json() -> str:
    return json.dumps({"amount_tolerance_paise": DEFAULT_MATCH_CONFIG.amount_tolerance_paise})


def _open(tmp_path: Path):
    return connect(tmp_path / "ledgerloop.db")


# --- schema ---------------------------------------------------------------------


def test_schema_creates_every_table(tmp_path: Path) -> None:
    with _open(tmp_path) as conn:
        initialise(conn)
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        names = {row[0] for row in rows}

    assert names >= EXPECTED_TABLES


def test_schema_indexes_the_fingerprint_columns(tmp_path: Path) -> None:
    """Idempotency looks up row_sha256 once per incoming row. Unindexed, that is a
    full scan per row — quadratic in batch size."""
    with _open(tmp_path) as conn:
        initialise(conn)
        rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        names = {row[0] for row in rows}

    assert names >= EXPECTED_FINGERPRINT_INDEXES


def test_initialise_is_idempotent(tmp_path: Path) -> None:
    """`make demo` re-runs against an existing database; applying the schema twice
    must not error and must not drop anything."""
    with _open(tmp_path) as conn:
        initialise(conn)
        start_run(conn, run_id="r1", fixture="easy", tiers_enabled="0", config_json="{}")
        initialise(conn)

        assert run_exists(conn, "r1")


# --- pragmas --------------------------------------------------------------------


def test_write_ahead_logging_is_enabled(tmp_path: Path) -> None:
    with _open(tmp_path) as conn:
        initialise(conn)
        mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()

    assert str(mode).lower() == "wal"


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    """SQLite disables foreign keys by default, per connection.

    Without this pragma a quarantine row could reference a run that never existed,
    and the run report would silently lose it.
    """
    with _open(tmp_path) as conn:
        initialise(conn)
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO quarantine "
                    "(run_id, source_file, line_number, raw_row, error, created_at) "
                    "VALUES ('no-such-run', 'f.csv', 2, 'raw', 'err', '2026-08-25')"
                )
            )


# --- runs -----------------------------------------------------------------------


def test_start_run_records_the_config_in_force(tmp_path: Path) -> None:
    """§9.1 requires reporting the MatchConfig alongside any metric. A metric whose
    tolerances are unknown is not reproducible."""
    with _open(tmp_path) as conn:
        initialise(conn)
        start_run(
            conn,
            run_id="demo",
            fixture="adversarial",
            tiers_enabled="0,1,2",
            config_json=_config_json(),
        )
        row = conn.execute(
            text(
                "SELECT fixture, tiers_enabled, config_json, degraded "
                "FROM runs WHERE run_id='demo'"
            )
        ).one()

    assert row.fixture == "adversarial"
    assert row.tiers_enabled == "0,1,2"
    assert json.loads(row.config_json)["amount_tolerance_paise"] == 100
    assert row.degraded == 0


def test_run_exists_distinguishes_known_from_unknown(tmp_path: Path) -> None:
    with _open(tmp_path) as conn:
        initialise(conn)
        start_run(conn, run_id="known", fixture="easy", tiers_enabled="0", config_json="{}")

        assert run_exists(conn, "known")
        assert not run_exists(conn, "missing")


def test_finish_run_marks_a_degraded_run(tmp_path: Path) -> None:
    """§8: when Tier 3 is unavailable the batch still completes, and the run is
    flagged. A degraded run's auto-match rate is not comparable to a full one, so the
    flag has to survive into the metrics."""
    with _open(tmp_path) as conn:
        initialise(conn)
        start_run(conn, run_id="deg", fixture="easy", tiers_enabled="0,1,2", config_json="{}")
        finish_run(conn, "deg", degraded=True)
        row = conn.execute(
            text("SELECT degraded, finished_at FROM runs WHERE run_id='deg'")
        ).one()

    assert row.degraded == 1
    assert row.finished_at is not None


def test_starting_the_same_run_twice_is_rejected(tmp_path: Path) -> None:
    """Runs are comparable objects. Silently reusing an id would merge two runs'
    rows and make the ablation diff meaningless."""
    with _open(tmp_path) as conn:
        initialise(conn)
        start_run(conn, run_id="dup", fixture="easy", tiers_enabled="0", config_json="{}")
        with pytest.raises(IntegrityError):
            start_run(conn, run_id="dup", fixture="easy", tiers_enabled="0", config_json="{}")
