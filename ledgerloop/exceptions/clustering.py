"""Group exceptions by reason code and merchant.

This turns the queue from a to-do list into a diagnostic instrument. Twelve exceptions
sharing one code and one merchant is not twelve problems — it is one wrong assumption,
usually in the fee model.

Sort the queue by RUPEE VALUE AT RISK, never by row order. An associate with twenty
minutes should spend them on the large exception.

**On "merchant".** §6 asks for clustering by reason code *and* merchant. Settlements
carry no ``merchant_id`` — only invoices do — so that join is not available here. Codes
cluster on their own, and the counterparty spread inside each cluster is reported as the
secondary signal. On a single-merchant fixture the two are equivalent; a multi-merchant
batch would need the join through ``invoice_ref``, and this is where it would go.

**A single exception is not a pattern.** One row is one row. Reporting it as a cluster
would manufacture a diagnosis out of noise, which is the opposite of what the queue is
for.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import Connection, text

from ledgerloop.exceptions.codes import ExceptionCode

#: What a human should actually do, per reason code. §6 asks the queue to show a
#: suggested next action — a code that says what happened but not what to do about it
#: leaves the associate to reverse-engineer the cascade.
SUGGESTED_ACTION: dict[ExceptionCode, str] = {
    ExceptionCode.NO_CANDIDATE: (
        "Look for a settlement outside the date window, or confirm this credit came from "
        "somewhere other than the gateway."
    ),
    ExceptionCode.AMBIGUOUS_SUBSET: (
        "Choose between the attached explanations. Both add up exactly, so the system "
        "will not pick one for you."
    ),
    ExceptionCode.AMOUNT_BEYOND_TOLERANCE: (
        "Check whether the fee model is wrong for this merchant before treating it as a "
        "data problem — if several rows share this code, the model is the likelier cause."
    ),
    ExceptionCode.DATE_OUT_OF_WINDOW: (
        "Confirm the settlement cycle. A credit outside the window is usually a timing "
        "assumption that needs widening, not a mismatched payment."
    ),
    ExceptionCode.LOW_CONFIDENCE: (
        "Read the model's evidence and either confirm the match or reject it. The system "
        "declined to post it on its own."
    ),
    ExceptionCode.ORPHAN_CREDIT: (
        "Trace this credit outside the gateway — an out-of-band transfer, a refund "
        "reversal or interest. No settlement was found in its window."
    ),
    ExceptionCode.DUPLICATE_SUSPECTED: (
        "Confirm with the bank whether this credit was re-posted. The money may have "
        "arrived once and been reported twice."
    ),
    ExceptionCode.LLM_INVALID_OUTPUT: (
        "No action on the record itself — the model returned something unusable and the "
        "response was discarded. Investigate if this code is common."
    ),
    ExceptionCode.POOL_TOO_LARGE: (
        "Too many candidate settlements to search safely. Narrow the batch by date, or "
        "resolve by hand."
    ),
    ExceptionCode.MODEL_UNAVAILABLE: (
        "The run completed without Tier 3. Re-run once the model is reachable; nothing "
        "here is wrong with the data."
    ),
}

#: Above this, a shared reason code stops being coincidence and starts being a signal.
PATTERN_THRESHOLD = 3


@dataclass(frozen=True)
class QueueItem:
    """One open exception, as an associate sees it."""

    exception_id: str
    bank_txn_id: str | None
    settlement_id: str | None
    code: ExceptionCode
    value_at_risk_paise: int
    detail: dict[str, object]
    suggested_action: str


@dataclass(frozen=True)
class Cluster:
    """A reason code and everything sharing it."""

    code: ExceptionCode
    count: int
    value_at_risk_paise: int
    counterparties: tuple[str, ...]
    diagnosis: str


def open_exceptions(conn: Connection, run_id: str) -> list[QueueItem]:
    """Every unresolved exception, most valuable first.

    Ordering is by rupee at risk rather than by row order, because that is the only
    ordering that respects what an associate's twenty minutes are worth.
    """
    rows = conn.execute(
        text(
            "SELECT exception_id, bank_txn_id, settlement_id, reason_code, "
            "value_at_risk_paise, detail_json FROM exceptions "
            "WHERE run_id = :run AND resolved_at IS NULL "
            "ORDER BY value_at_risk_paise DESC, exception_id"
        ),
        {"run": run_id},
    ).all()

    items: list[QueueItem] = []
    for row in rows:
        code = ExceptionCode(row.reason_code)
        items.append(
            QueueItem(
                exception_id=row.exception_id,
                bank_txn_id=row.bank_txn_id,
                settlement_id=row.settlement_id,
                code=code,
                value_at_risk_paise=row.value_at_risk_paise,
                detail=json.loads(row.detail_json or "{}"),
                suggested_action=SUGGESTED_ACTION[code],
            )
        )
    return items


def cluster(items: list[QueueItem]) -> list[Cluster]:
    """Group by reason code, most valuable cluster first.

    By value rather than by count: twenty three-hundred-rupee exceptions matter less than
    one four-lakh exception, and ordering by count would put the noise at the top.
    """
    grouped: dict[ExceptionCode, list[QueueItem]] = {}
    for item in items:
        grouped.setdefault(item.code, []).append(item)

    clusters = [
        Cluster(
            code=code,
            count=len(members),
            value_at_risk_paise=sum(item.value_at_risk_paise for item in members),
            counterparties=_counterparties(members),
            diagnosis=_diagnose(code, members),
        )
        for code, members in grouped.items()
    ]
    return sorted(
        clusters, key=lambda item: (-item.value_at_risk_paise, item.code.value)
    )


def _counterparties(members: list[QueueItem]) -> tuple[str, ...]:
    """Distinct counterparties implicated, where the detail names any."""
    found = {
        str(item.detail.get("counterparty"))
        for item in members
        if item.detail.get("counterparty")
    }
    return tuple(sorted(found))


def _diagnose(code: ExceptionCode, members: list[QueueItem]) -> str:
    """Say what the cluster means, not merely how large it is.

    This is §8's strongest product argument made concrete: a group-by tells an associate
    there are twelve rows, a diagnosis tells them there is one problem.
    """
    if len(members) < PATTERN_THRESHOLD:
        return f"{len(members)} record(s). {SUGGESTED_ACTION[code]}"

    if code is ExceptionCode.AMOUNT_BEYOND_TOLERANCE:
        return (
            f"{len(members)} records share this code. That is not {len(members)} problems "
            "— it is most likely one wrong assumption in the fee model for this merchant. "
            "Correcting the model resolves the whole class at once."
        )
    if code is ExceptionCode.DATE_OUT_OF_WINDOW:
        return (
            f"{len(members)} records share this code, which points at one wrong timing "
            "assumption rather than many mismatched payments. Check the settlement lag "
            "before resolving these individually."
        )
    if code is ExceptionCode.LLM_INVALID_OUTPUT:
        return (
            f"{len(members)} responses were unusable. That is a systemic signal about the "
            "model or the prompt, not about the merchant's data."
        )
    return (
        f"{len(members)} records share this code. Look for a common cause before working "
        f"through them one at a time. {SUGGESTED_ACTION[code]}"
    )
