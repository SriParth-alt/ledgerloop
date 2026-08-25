"""Quarantine for rows that fail schema validation.

Stores raw text + validation error + source file + line number.

Quarantined rows appear in the run report. A batch that quarantines 40 rows and
reports a 95% match rate on the rest is lying by omission — the denominator has been
quietly shrunk to whatever happened to parse.

The raw text is kept verbatim rather than normalised. Whoever debugs a quarantined row
needs the bytes the bank actually sent, not our reading of them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class QuarantinedRow:
    """One row that could not be loaded, with everything needed to diagnose it."""

    source_file: str
    line_number: int
    raw_row: str
    error: str


def quarantine_row(
    conn: Connection,
    run_id: str,
    *,
    source_file: str,
    line_number: int,
    raw_row: str,
    error: str,
) -> None:
    """Record a row that failed validation. The batch continues without it."""
    conn.execute(
        text(
            "INSERT INTO quarantine "
            "(run_id, source_file, line_number, raw_row, error, created_at) "
            "VALUES (:run_id, :source_file, :line_number, :raw_row, :error, :created_at)"
        ),
        {
            "run_id": run_id,
            "source_file": source_file,
            "line_number": line_number,
            "raw_row": raw_row,
            "error": error,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )


def quarantined_for_run(conn: Connection, run_id: str) -> list[QuarantinedRow]:
    """Every quarantined row for a run, in the order encountered."""
    rows = conn.execute(
        text(
            "SELECT source_file, line_number, raw_row, error FROM quarantine "
            "WHERE run_id = :run_id ORDER BY quarantine_id"
        ),
        {"run_id": run_id},
    ).all()
    return [
        QuarantinedRow(
            source_file=row.source_file,
            line_number=row.line_number,
            raw_row=row.raw_row,
            error=row.error,
        )
        for row in rows
    ]
