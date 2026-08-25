"""Fingerprinting and idempotent load.

SHA-256 of each raw row, canonicalised — stripped, keys lowercased, sorted — used as
the natural key. Re-ingesting the same file is a no-op, and DUPLICATE_POST chaos
surfaces as ``DUPLICATE_SUSPECTED`` rather than silently double-counting money.

Two things that look alike and are not:

* **The same row arriving twice** is idempotency. Its fingerprint already exists, so it
  is skipped silently. This is what makes ``make demo`` safe to re-run.
* **The same money under a different identity** is a re-post. Its fingerprint is new —
  the transaction id differs — so it loads, and then raises ``DUPLICATE_SUSPECTED``
  with the money at risk attached.

Absorbing the second case would lose money silently; flagging the first would fill the
queue with noise on every re-run. The fingerprint separates them.

The row is never dropped. Tables are append-only and deleting a bank row is not ours to
do — what makes a suspected duplicate safe is that a human sees it, not that we hid it.
"""

from __future__ import annotations

import csv
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ValidationError
from sqlalchemy import Connection, text

from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.ingest.quarantine import quarantine_row
from ledgerloop.ingest.schemas import BankRow, InvoiceRow, SettlementRow

#: Separators chosen so no key or value can impersonate the structure. A naive
#: ``f"{k}={v}"`` join collides: ``{"a": "b=c"}`` and ``{"a=b": "c"}`` serialise
#: identically, and two genuinely different bank rows would deduplicate each other.
_FIELD_SEPARATOR = "\x1f"
_PAIR_SEPARATOR = "\x1e"


@dataclass(frozen=True)
class LoadReport:
    """What one source file contributed to a run."""

    source_file: str
    inserted: int
    skipped_idempotent: int
    quarantined: int
    duplicate_suspected: tuple[str, ...] = ()


def canonical_row_fingerprint(row: Mapping[str, str]) -> str:
    """SHA-256 of a canonicalised row.

    Keys are stripped and lowercased because column naming is a property of the
    export, not of the data. Values are stripped but **not** case-folded: ``ACME`` and
    ``acme`` in a narration are genuinely different evidence, and collapsing them would
    let two distinct rows deduplicate each other.
    """
    pairs = sorted(
        (key.strip().lower(), (value or "").strip()) for key, value in row.items()
    )
    payload = _FIELD_SEPARATOR.join(
        f"{key}{_PAIR_SEPARATOR}{value}" for key, value in pairs
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _money_signature(row: BankRow) -> str:
    """What makes two bank rows 'the same money'.

    Narration is part of the signature deliberately. Amount and date alone would flag
    every ``DECOY_SUBSET`` pair as a re-post, since a decoy is *designed* to put two
    identical amounts on one date. A genuine re-post is character-identical in its
    narration; two customers who happened to pay alike are not.
    """
    return _FIELD_SEPARATOR.join(
        (str(row.credit_paise), row.value_date.isoformat(), row.narration)
    )


def _seen_fingerprints(conn: Connection, table: str, run_id: str) -> set[str]:
    rows = conn.execute(
        text(f"SELECT row_sha256 FROM {table} WHERE run_id = :run_id"), {"run_id": run_id}
    )
    return {row[0] for row in rows}


def _read_rows(path: Path) -> list[tuple[int, dict[str, str], str]]:
    """Yield ``(line_number, row, raw_text)`` for each data line.

    ``line_number`` is 1-based over the physical file including the header, so it
    matches what a person sees when they open the CSV to look.
    """
    text_content = path.read_text(encoding="utf-8")
    lines = text_content.splitlines()
    if not lines:
        return []

    reader = csv.DictReader(lines)
    out: list[tuple[int, dict[str, str], str]] = []
    for offset, row in enumerate(reader, start=2):
        cleaned = {key: (value or "") for key, value in row.items() if key is not None}
        out.append((offset, cleaned, lines[offset - 1]))
    return out


def _load_file(
    conn: Connection,
    run_id: str,
    path: Path,
    *,
    model: type[BaseModel],
    table: str,
    insert_sql: str,
    to_params: object,
) -> tuple[LoadReport, list[BankRow]]:
    seen = _seen_fingerprints(conn, table, run_id)
    inserted = skipped = quarantined = 0
    validated: list[BankRow] = []

    for line_number, raw_row, raw_text in _read_rows(path):
        fingerprint = canonical_row_fingerprint(raw_row)
        if fingerprint in seen:
            skipped += 1
            continue

        try:
            record = model.model_validate(raw_row)
        except ValidationError as error:
            quarantine_row(
                conn,
                run_id,
                source_file=path.name,
                line_number=line_number,
                raw_row=raw_text,
                error=str(error),
            )
            quarantined += 1
            continue

        conn.execute(text(insert_sql), to_params(record, run_id, fingerprint))  # type: ignore[operator]
        seen.add(fingerprint)
        inserted += 1
        if isinstance(record, BankRow):
            validated.append(record)

    return (
        LoadReport(
            source_file=path.name,
            inserted=inserted,
            skipped_idempotent=skipped,
            quarantined=quarantined,
        ),
        validated,
    )


def _invoice_params(row: InvoiceRow, run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "invoice_id": row.invoice_id,
        "run_id": run_id,
        "row_sha256": fingerprint,
        "merchant_id": row.merchant_id,
        "customer_id": row.customer_id,
        "customer_name": row.customer_name,
        "invoice_amount_paise": row.invoice_amount_paise,
        "currency": row.currency,
        "issue_date": row.issue_date.isoformat(),
        "due_date": row.due_date.isoformat(),
        "status": row.status,
    }


def _settlement_params(row: SettlementRow, run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "settlement_id": row.settlement_id,
        "run_id": run_id,
        "row_sha256": fingerprint,
        "payment_id": row.payment_id,
        "order_id": row.order_id,
        "invoice_ref": row.invoice_ref,
        "customer_name": row.customer_name,
        "method": str(row.method),
        "gross_amount_paise": row.gross_amount_paise,
        "fee_paise": row.fee_paise,
        "gst_on_fee_paise": row.gst_on_fee_paise,
        "tds_paise": row.tds_paise,
        "net_amount_paise": row.net_amount_paise,
        "captured_at": row.captured_at.isoformat(),
        "settled_on": row.settled_on.isoformat(),
        "utr": row.utr,
        "status": row.status,
    }


def _bank_params(row: BankRow, run_id: str, fingerprint: str) -> dict[str, object]:
    return {
        "bank_txn_id": row.bank_txn_id,
        "run_id": run_id,
        "row_sha256": fingerprint,
        "value_date": row.value_date.isoformat(),
        "narration": row.narration,
        "credit_paise": row.credit_paise,
        "debit_paise": row.debit_paise,
        "balance_paise": row.balance_paise,
    }


_INVOICE_SQL = (
    "INSERT INTO invoices (invoice_id, run_id, row_sha256, merchant_id, customer_id, "
    "customer_name, invoice_amount_paise, currency, issue_date, due_date, status) "
    "VALUES (:invoice_id, :run_id, :row_sha256, :merchant_id, :customer_id, "
    ":customer_name, :invoice_amount_paise, :currency, :issue_date, :due_date, :status)"
)

_SETTLEMENT_SQL = (
    "INSERT INTO settlements (settlement_id, run_id, row_sha256, payment_id, order_id, "
    "invoice_ref, customer_name, method, gross_amount_paise, fee_paise, gst_on_fee_paise, "
    "tds_paise, net_amount_paise, captured_at, settled_on, utr, status) "
    "VALUES (:settlement_id, :run_id, :row_sha256, :payment_id, :order_id, :invoice_ref, "
    ":customer_name, :method, :gross_amount_paise, :fee_paise, :gst_on_fee_paise, "
    ":tds_paise, :net_amount_paise, :captured_at, :settled_on, :utr, :status)"
)

_BANK_SQL = (
    "INSERT INTO bank_txns (bank_txn_id, run_id, row_sha256, value_date, narration, "
    "credit_paise, debit_paise, balance_paise) "
    "VALUES (:bank_txn_id, :run_id, :row_sha256, :value_date, :narration, "
    ":credit_paise, :debit_paise, :balance_paise)"
)


def load_invoices(conn: Connection, run_id: str, path: Path) -> LoadReport:
    report, _ = _load_file(
        conn,
        run_id,
        path,
        model=InvoiceRow,
        table="invoices",
        insert_sql=_INVOICE_SQL,
        to_params=_invoice_params,
    )
    return report


def load_settlements(conn: Connection, run_id: str, path: Path) -> LoadReport:
    report, _ = _load_file(
        conn,
        run_id,
        path,
        model=SettlementRow,
        table="settlements",
        insert_sql=_SETTLEMENT_SQL,
        to_params=_settlement_params,
    )
    return report


def load_bank_statement(conn: Connection, run_id: str, path: Path) -> LoadReport:
    """Load bank credits and flag any that re-post money already seen in this run."""
    report, freshly_loaded = _load_file(
        conn,
        run_id,
        path,
        model=BankRow,
        table="bank_txns",
        insert_sql=_BANK_SQL,
        to_params=_bank_params,
    )
    flagged = _flag_reposted_credits(conn, run_id, freshly_loaded)
    return LoadReport(
        source_file=report.source_file,
        inserted=report.inserted,
        skipped_idempotent=report.skipped_idempotent,
        quarantined=report.quarantined,
        duplicate_suspected=flagged,
    )


def _flag_reposted_credits(
    conn: Connection, run_id: str, rows: list[BankRow]
) -> tuple[str, ...]:
    """Raise ``DUPLICATE_SUSPECTED`` for credits that repeat money already loaded.

    Only rows inserted by *this* call are considered, so a re-ingest — where nothing is
    inserted — raises nothing. Without that, every re-run would append another exception
    for the same re-post and the queue would grow on each pass.
    """
    if not rows:
        return ()

    already_flagged = {
        row[0]
        for row in conn.execute(
            text(
                "SELECT bank_txn_id FROM exceptions "
                "WHERE run_id = :run_id AND reason_code = :code"
            ),
            {"run_id": run_id, "code": ExceptionCode.DUPLICATE_SUSPECTED.value},
        )
    }

    by_signature: dict[str, list[BankRow]] = {}
    for row in rows:
        if row.credit_paise > 0:
            by_signature.setdefault(_money_signature(row), []).append(row)

    flagged: list[str] = []
    for group in by_signature.values():
        if len(group) < 2:
            continue
        # The earliest row is the original; everything after it re-posts that money.
        for duplicate in group[1:]:
            if duplicate.bank_txn_id in already_flagged:
                continue
            _record_duplicate_exception(conn, run_id, duplicate, original=group[0].bank_txn_id)
            flagged.append(duplicate.bank_txn_id)
    return tuple(flagged)


def _record_duplicate_exception(
    conn: Connection, run_id: str, duplicate: BankRow, *, original: str
) -> None:
    conn.execute(
        text(
            "INSERT INTO exceptions (exception_id, run_id, bank_txn_id, reason_code, "
            "value_at_risk_paise, detail_json, created_at) "
            "VALUES (:exception_id, :run_id, :bank_txn_id, :reason_code, "
            ":value_at_risk_paise, :detail_json, :created_at)"
        ),
        {
            "exception_id": str(uuid.uuid4()),
            "run_id": run_id,
            "bank_txn_id": duplicate.bank_txn_id,
            "reason_code": ExceptionCode.DUPLICATE_SUSPECTED.value,
            "value_at_risk_paise": duplicate.credit_paise,
            "detail_json": json.dumps(
                {
                    "duplicates_bank_txn_id": original,
                    "value_date": duplicate.value_date.isoformat(),
                    "narration": duplicate.narration,
                    "note": (
                        "Same amount, value date and narration as an earlier credit in "
                        "this run, under a different transaction id."
                    ),
                }
            ),
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )


def load_batch(
    conn: Connection,
    run_id: str,
    *,
    invoices: Path,
    settlements: Path,
    bank_statement: Path,
) -> dict[str, LoadReport]:
    """Load all three source files for a run.

    Paths are named explicitly rather than taken as a mapping. The generator returns a
    mapping that also contains the ground-truth file, and iterating it here would load
    truth into the matcher's own tables — the exact leak ``tests/test_no_truth_leak.py``
    exists to prevent. Naming the three inputs makes that impossible to do by accident.
    """
    return {
        "invoices": load_invoices(conn, run_id, invoices),
        "settlements": load_settlements(conn, run_id, settlements),
        "bank_statement": load_bank_statement(conn, run_id, bank_statement),
    }
