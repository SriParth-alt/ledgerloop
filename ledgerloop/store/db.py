"""SQLite connection management.

WAL mode, foreign keys ON, and a ``run_id`` on every write so a reconciliation run is
a first-class, comparable object.

Tables are APPEND-ONLY. Corrections are new rows superseding old ones, never
UPDATEs. Provenance depends on this — an UPDATE destroys the audit trail you
are going to demo.

Two pragmas carry real weight and both are per-connection, which is why they are set
by an engine-level event listener rather than by the ``PRAGMA`` lines in
``schema.sql``: those run once at creation, while a listener runs for every connection
the pool hands out. ``foreign_keys`` is **off** by default in SQLite, so without it the
``REFERENCES runs(run_id)`` clauses throughout the schema are decorative.

``finish_run`` is the one deliberate exception to append-only. A run row is opened when
the run starts and closed when it ends; that is a lifecycle field on the run itself,
not a correction to a matching decision, and the audit trail lives in ``match_records``
and ``exceptions`` rather than here.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Connection, create_engine, event, text
from sqlalchemy.engine import Engine

from ledgerloop.ingest.schemas import BankRow, SettlementRow

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB_PATH = Path("ledgerloop.db")


def _apply_pragmas(dbapi_connection: sqlite3.Connection, _record: object) -> None:
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA foreign_keys = ON")
    finally:
        cursor.close()


def create_db_engine(db_path: Path = DEFAULT_DB_PATH) -> Engine:
    """Build an engine with the pragmas wired to every pooled connection."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite+pysqlite:///{path}", future=True)
    event.listen(engine, "connect", _apply_pragmas)
    return engine


@contextmanager
def connect(db_path: Path = DEFAULT_DB_PATH) -> Iterator[Connection]:
    """Yield a connection that commits on clean exit and rolls back on error.

    A half-ingested batch is worse than a failed one: the fingerprints of the rows
    that made it in would make a retry look like a no-op for exactly those rows.
    """
    engine = create_db_engine(db_path)
    connection = engine.connect()
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
        engine.dispose()


def _schema_statements(sql: str) -> list[str]:
    """Split ``schema.sql`` into executable statements.

    Comments are stripped *before* splitting on ``;`` because the file's own prose
    contains one — "corrections are new rows that supersede old ones; there are no
    UPDATEs" — and splitting first cuts that sentence in half, handing SQLite the
    remainder as SQL. (This assumes no ``--`` inside a string literal, which holds for
    this schema and is checked by the tests applying the real file.)

    ``PRAGMA`` lines are skipped. They are applied per connection by the engine
    listener above, and ``journal_mode = WAL`` cannot be set from inside a
    transaction, which is where this runs.
    """
    uncommented = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    statements = []
    for chunk in uncommented.split(";"):
        statement = chunk.strip()
        if statement and not statement.upper().startswith("PRAGMA"):
            statements.append(statement)
    return statements


def initialise(conn: Connection) -> None:
    """Apply ``schema.sql``. Safe to call against an existing database.

    Every statement is ``CREATE ... IF NOT EXISTS``, so re-running ``make demo`` over a
    populated database adds nothing and drops nothing.
    """
    for statement in _schema_statements(SCHEMA_PATH.read_text(encoding="utf-8")):
        conn.execute(text(statement))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def start_run(
    conn: Connection,
    *,
    run_id: str,
    fixture: str,
    tiers_enabled: str,
    config_json: str,
    notes: str | None = None,
) -> str:
    """Open a run. Re-using an existing ``run_id`` raises rather than merging.

    ``config_json`` is the ``MatchConfig`` in force. Section 9.1 requires reporting it
    alongside any metric — a number whose tolerances are unknown is not reproducible.
    """
    conn.execute(
        text(
            "INSERT INTO runs (run_id, started_at, fixture, tiers_enabled, config_json, notes) "
            "VALUES (:run_id, :started_at, :fixture, :tiers_enabled, :config_json, :notes)"
        ),
        {
            "run_id": run_id,
            "started_at": _now(),
            "fixture": fixture,
            "tiers_enabled": tiers_enabled,
            "config_json": config_json,
            "notes": notes,
        },
    )
    return run_id


def finish_run(
    conn: Connection,
    run_id: str,
    *,
    degraded: bool = False,
    tiers_enabled: str | None = None,
) -> None:
    """Close a run, recording whether Tier 3 was unavailable.

    A degraded run's auto-match rate is not comparable to a full one, so the flag has
    to survive into the metrics rather than living only in a log line.

    ``tiers_enabled`` is rewritten with what actually executed. The value set at
    ``start_run`` is an intention; the ablation table is labelled with this one, and a
    run whose label disagrees with its behaviour would poison every comparison drawn
    from it.
    """
    if tiers_enabled is None:
        conn.execute(
            text(
                "UPDATE runs SET finished_at = :finished_at, degraded = :degraded "
                "WHERE run_id = :id"
            ),
            {"finished_at": _now(), "degraded": int(degraded), "id": run_id},
        )
        return

    conn.execute(
        text(
            "UPDATE runs SET finished_at = :finished_at, degraded = :degraded, "
            "tiers_enabled = :tiers WHERE run_id = :id"
        ),
        {
            "finished_at": _now(),
            "degraded": int(degraded),
            "tiers": tiers_enabled,
            "id": run_id,
        },
    )


def load_bank_txns(conn: Connection, run_id: str) -> list[BankRow]:
    """Every bank row in a run, in file order."""
    rows = conn.execute(
        text(
            "SELECT bank_txn_id, value_date, narration, credit_paise, debit_paise, "
            "balance_paise FROM bank_txns WHERE run_id = :run ORDER BY rowid"
        ),
        {"run": run_id},
    ).mappings()
    return [BankRow.model_validate(dict(row)) for row in rows]


def load_settlements(conn: Connection, run_id: str) -> list[SettlementRow]:
    """Every settlement in a run, in file order."""
    rows = conn.execute(
        text(
            "SELECT settlement_id, payment_id, order_id, invoice_ref, customer_name, method, "
            "gross_amount_paise, fee_paise, gst_on_fee_paise, tds_paise, net_amount_paise, "
            "captured_at, settled_on, utr, status FROM settlements "
            "WHERE run_id = :run ORDER BY rowid"
        ),
        {"run": run_id},
    ).mappings()
    return [SettlementRow.model_validate(dict(row)) for row in rows]


def matched_bank_txn_ids(conn: Connection, run_id: str) -> set[str]:
    """Bank rows already explained by a posted match in this run."""
    rows = conn.execute(
        text("SELECT bank_txn_id FROM match_records WHERE run_id = :run"), {"run": run_id}
    )
    return {row[0] for row in rows}


def matched_settlement_ids(conn: Connection, run_id: str) -> set[str]:
    """Settlements already spent by a posted match in this run.

    A settlement claimed twice is money counted twice, and no later tier recomputes
    it — so every tier filters against this before proposing anything.
    """
    rows = conn.execute(
        text("SELECT settlement_ids_json FROM match_records WHERE run_id = :run"),
        {"run": run_id},
    )
    return {
        settlement_id for row in rows for settlement_id in json.loads(row[0])
    }


def run_exists(conn: Connection, run_id: str) -> bool:
    found = conn.execute(
        text("SELECT 1 FROM runs WHERE run_id = :id"), {"id": run_id}
    ).first()
    return found is not None
