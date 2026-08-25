"""Fingerprinting, idempotent load, duplicate detection, and quarantine.

Day 3's done-condition is one sentence: **re-running the same file is a no-op.** Most
of this file exists to hold that line, because the failure mode is silent — a loader
that re-inserts on every run doubles the money in the ledger and nothing complains.

The other half is the distinction the loader TODO draws, which is subtler than it
looks:

* the *same row* arriving twice is idempotency, and is absorbed silently;
* the *same money under a different identity* is a re-post, and must surface as
  DUPLICATE_SUSPECTED rather than being quietly deduplicated.

Getting that backwards in either direction is a real bug. Absorbing a re-post loses
money silently; flagging a genuine re-ingest fills the queue with noise.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import text

from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.synth import generate_fixture
from ledgerloop.ingest.loader import canonical_row_fingerprint, load_batch
from ledgerloop.ingest.quarantine import quarantined_for_run
from ledgerloop.store.db import connect, initialise, start_run

RUN_ID = "test-run"
SOURCE_TABLES = ("invoices", "settlements", "bank_txns")
COUNTED_TABLES = (*SOURCE_TABLES, "exceptions", "quarantine")

MONEY_COLUMNS = (
    ("invoices", "invoice_amount_paise"),
    ("settlements", "gross_amount_paise"),
    ("settlements", "fee_paise"),
    ("settlements", "net_amount_paise"),
    ("bank_txns", "credit_paise"),
    ("bank_txns", "balance_paise"),
)


def _fixture_files(tmp_path: Path) -> dict[str, Path]:
    return generate_fixture(fixture="adversarial", settlements=60, seed=42, out_dir=tmp_path)


def _load(conn, paths: dict[str, Path]):
    """Note what is *not* passed: the ground-truth file. `load_batch` takes explicit
    named paths rather than the generator's mapping, so truth cannot reach the loader
    by accident."""
    return load_batch(
        conn,
        RUN_ID,
        invoices=paths["invoices"],
        settlements=paths["settlements"],
        bank_statement=paths["bank_statement"],
    )


def _counts(conn) -> dict[str, int]:
    return {
        table: conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        for table in COUNTED_TABLES
    }


def _corrupt_data_line(path: Path, line_number: int, replacement: str) -> None:
    lines = path.read_text(encoding="utf-8").split("\n")
    lines[line_number - 1] = replacement
    path.write_text("\n".join(lines), encoding="utf-8")


@pytest.fixture
def loaded(tmp_path: Path):
    paths = _fixture_files(tmp_path)
    with connect(tmp_path / "ledgerloop.db") as conn:
        initialise(conn)
        start_run(conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="0", config_json="{}")
        reports = _load(conn, paths)
        yield conn, paths, reports


# --- fingerprinting -------------------------------------------------------------


def test_fingerprint_is_stable_across_key_order() -> None:
    """CSV column order is not a property of the data. A reordered export is the same
    row and must not re-insert."""
    assert canonical_row_fingerprint({"b": "2", "a": "1"}) == canonical_row_fingerprint(
        {"a": "1", "b": "2"}
    )


def test_fingerprint_ignores_surrounding_whitespace_and_key_case() -> None:
    assert canonical_row_fingerprint({" Amount ": " 100 "}) == canonical_row_fingerprint(
        {"amount": "100"}
    )


def test_fingerprint_preserves_value_case() -> None:
    """Keys are normalised; values are not. 'ACME' and 'acme' in a narration are
    genuinely different evidence, and collapsing them would let two distinct bank rows
    deduplicate each other."""
    assert canonical_row_fingerprint({"narration": "ACME"}) != canonical_row_fingerprint(
        {"narration": "acme"}
    )


def test_fingerprint_changes_when_any_value_changes() -> None:
    base = {"bank_txn_id": "BNK1", "credit_paise": "1000"}
    assert canonical_row_fingerprint(base) != canonical_row_fingerprint(
        {**base, "credit_paise": "1001"}
    )


def test_fingerprint_cannot_be_forged_by_shifting_a_delimiter() -> None:
    """A naive `f"{k}={v}"` join collides: {'a': 'b=c'} and {'a=b': 'c'} serialise
    identically. Two different bank rows would then deduplicate each other and one
    credit would vanish from the ledger."""
    assert canonical_row_fingerprint({"a": "b=c"}) != canonical_row_fingerprint({"a=b": "c"})


# --- the done-condition ---------------------------------------------------------


def test_loading_a_batch_inserts_every_source_row(loaded) -> None:
    conn, paths, reports = loaded
    for key, table in (
        ("invoices", "invoices"),
        ("settlements", "settlements"),
        ("bank_statement", "bank_txns"),
    ):
        expected = len(paths[key].read_text(encoding="utf-8").strip().split("\n")) - 1
        actual = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
        assert actual == expected, f"{table}: loaded {actual} of {expected}"

    assert all(report.quarantined == 0 for report in reports.values())


def test_reingesting_the_same_files_is_a_no_op(loaded) -> None:
    """Day 3's done-condition, and the reason the fingerprint exists.

    Exceptions are counted too: a second pass must not re-raise DUPLICATE_SUSPECTED
    for a re-post it already flagged, or every re-run inflates the queue.
    """
    conn, paths, _ = loaded
    before = _counts(conn)

    _load(conn, paths)

    assert _counts(conn) == before


def test_reingest_reports_rows_as_skipped_rather_than_inserted(loaded) -> None:
    conn, paths, first = loaded
    second = _load(conn, paths)

    for key in first:
        assert second[key].inserted == 0
        assert second[key].skipped_idempotent == first[key].inserted


def test_a_different_run_loads_the_same_data_again(loaded) -> None:
    """Idempotency is scoped *within* a run. Runs are comparable objects, so each one
    holds its own copy of the source rows — that is what lets the ablation diff two
    runs over the same fixture."""
    conn, paths, _ = loaded
    start_run(conn, run_id="second", fixture="adversarial", tiers_enabled="0", config_json="{}")

    load_batch(
        conn,
        "second",
        invoices=paths["invoices"],
        settlements=paths["settlements"],
        bank_statement=paths["bank_statement"],
    )
    per_run = conn.execute(
        text("SELECT run_id, COUNT(*) AS n FROM bank_txns GROUP BY run_id")
    ).all()

    assert len(per_run) == 2
    assert len({row.n for row in per_run}) == 1


# --- duplicate detection --------------------------------------------------------


def _duplicate_exceptions(conn):
    return conn.execute(
        text(
            "SELECT bank_txn_id, value_at_risk_paise FROM exceptions "
            "WHERE reason_code = :code"
        ),
        {"code": ExceptionCode.DUPLICATE_SUSPECTED.value},
    ).all()


def test_reposted_credit_raises_exactly_one_duplicate_suspected(loaded) -> None:
    conn, _, _ = loaded
    flagged = _duplicate_exceptions(conn)

    assert len(flagged) == 1


def test_reposted_credit_is_stored_and_flagged_not_silently_absorbed(loaded) -> None:
    """The row is kept — the tables are append-only and dropping data is not ours to
    do. What makes it safe is that a human sees it, with the money at risk attached."""
    conn, _, _ = loaded
    flagged = _duplicate_exceptions(conn)
    txn_id, value_at_risk = flagged[0]

    stored = conn.execute(
        text("SELECT credit_paise FROM bank_txns WHERE bank_txn_id = :i"), {"i": txn_id}
    ).scalar_one()

    assert value_at_risk == stored


def test_decoy_subsets_do_not_trigger_duplicate_suspicion(loaded) -> None:
    """DECOY_SUBSET deliberately creates two credits with identical amounts, often on
    the same date. Keying duplicate detection on (amount, date) alone would flag every
    decoy pair as a re-post — the narration is what separates a genuine re-post from
    two customers who happened to pay the same amount.
    """
    conn, _, _ = loaded
    same_money_different_story = conn.execute(
        text(
            "SELECT DISTINCT a.bank_txn_id FROM bank_txns a JOIN bank_txns b "
            "  ON a.credit_paise = b.credit_paise AND a.value_date = b.value_date "
            " AND a.bank_txn_id <> b.bank_txn_id AND a.narration <> b.narration"
        )
    ).all()
    assert same_money_different_story, "fixture no longer exercises the false-positive case"

    flagged = {row.bank_txn_id for row in _duplicate_exceptions(conn)}

    assert {row.bank_txn_id for row in same_money_different_story} & flagged == set()


# --- quarantine -----------------------------------------------------------------


def test_malformed_row_is_quarantined_with_its_line_number(tmp_path: Path) -> None:
    """§8: a malformed row is quarantined with its raw content and error; the batch
    continues. Losing one row must never cost you the other 249."""
    paths = _fixture_files(tmp_path)
    _corrupt_data_line(
        paths["bank_statement"], 3, "BNKBAD01,2026-08-10,BAD ROW,not-a-number,0,100"
    )

    with connect(tmp_path / "ledgerloop.db") as conn:
        initialise(conn)
        start_run(conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="0", config_json="{}")
        reports = _load(conn, paths)
        quarantined = quarantined_for_run(conn, RUN_ID)

    assert reports["bank_statement"].quarantined == 1
    assert len(quarantined) == 1
    assert quarantined[0].line_number == 3
    assert "not-a-number" in quarantined[0].raw_row
    assert quarantined[0].error


def test_one_malformed_row_does_not_cost_the_others(tmp_path: Path) -> None:
    paths = _fixture_files(tmp_path)
    total = len(paths["bank_statement"].read_text(encoding="utf-8").strip().split("\n")) - 1
    _corrupt_data_line(
        paths["bank_statement"], 3, "BNKBAD01,2026-08-10,BAD ROW,not-a-number,0,100"
    )

    with connect(tmp_path / "ledgerloop.db") as conn:
        initialise(conn)
        start_run(conn, run_id=RUN_ID, fixture="adversarial", tiers_enabled="0", config_json="{}")
        _load(conn, paths)
        loaded_rows = conn.execute(text("SELECT COUNT(*) FROM bank_txns")).scalar_one()

    assert loaded_rows == total - 1


# --- money integrity ------------------------------------------------------------


def test_no_float_reaches_any_money_column(loaded) -> None:
    """Design rule 5, checked where it actually matters — after the value has crossed
    the storage boundary. SQLite stores whatever it is handed, so a float in the
    loader would land here as REAL and nothing else would notice."""
    conn, _, _ = loaded
    for table, column in MONEY_COLUMNS:
        kinds = {
            row[0]
            for row in conn.execute(text(f"SELECT DISTINCT typeof({column}) FROM {table}"))
        }
        assert kinds <= {"integer"}, f"{table}.{column} stored as {kinds}"
