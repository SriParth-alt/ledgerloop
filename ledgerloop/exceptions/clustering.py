"""Group exceptions by reason code and merchant.

This turns the queue from a to-do list into a diagnostic instrument. Twelve exceptions
sharing one code and one merchant is not twelve problems — it is one wrong assumption,
usually in the fee model.

Sort the queue by RUPEE VALUE AT RISK, never by row order. An associate with twenty
minutes should spend them on the large exception.

**One item per credit.** A credit can carry several open reasons — Tier 2 declines it as
POOL_TOO_LARGE, it falls through, and Tier 3's gate refuses the model's proposal. That is
one thing to look at and one sum of money at risk, so the queue shows it once (ADR-042).

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
from typing import Any

from sqlalchemy import Connection, text

from ledgerloop.exceptions.codes import RAISED_BY_TIER3, ExceptionCode

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

#: AMOUNT_BEYOND_TOLERANCE when Tier 3's arithmetic gate raised it. The code is the same;
#: what happened is not, and neither is what the associate should do.
MODEL_PROPOSAL_DID_NOT_ADD_UP = (
    "The model named settlements that do not add up to this credit, and Python refused the "
    "match. Find the settlements that do explain it — this is not evidence that the fee "
    "model is wrong."
)

#: Above this, a shared reason code stops being coincidence and starts being a signal.
PATTERN_THRESHOLD = 3


@dataclass(frozen=True)
class QueueItem:
    """One credit with an open exception, as an associate sees it.

    ``code`` is the latest reason and ``exception_id`` the latest exception — the one to
    pass to ``resolve``, which closes every open reason on the credit. ``history`` holds
    the earlier reasons, oldest first, so a Tier 2 decline is not lost behind the Tier 3
    rejection that followed it.
    """

    exception_id: str
    bank_txn_id: str | None
    settlement_id: str | None
    code: ExceptionCode
    value_at_risk_paise: int
    detail: dict[str, object]
    suggested_action: str
    history: tuple[ExceptionCode, ...] = ()


@dataclass(frozen=True)
class Cluster:
    """A reason code and everything sharing it."""

    code: ExceptionCode
    count: int
    value_at_risk_paise: int
    counterparties: tuple[str, ...]
    diagnosis: str


def suggested_action(code: ExceptionCode, detail: dict[str, object]) -> str:
    """What to do about one exception, which can depend on who raised it.

    From a deterministic tier, AMOUNT_BEYOND_TOLERANCE points at the fee model. From Tier
    3's arithmetic gate it means the model's proposal did not add up, and advising a fee
    model check sends the associate to the wrong place (ADR-042).
    """
    if (
        code is ExceptionCode.AMOUNT_BEYOND_TOLERANCE
        and detail.get("raised_by") == RAISED_BY_TIER3
    ):
        return MODEL_PROPOSAL_DID_NOT_ADD_UP
    return SUGGESTED_ACTION[code]


def open_exceptions(conn: Connection, run_id: str) -> list[QueueItem]:
    """Every credit with an unresolved exception, most valuable first.

    One item per credit, showing its latest reason. Listing rows instead counted a credit
    once per tier that declined it — its money at risk twice — so the queue's total
    disagreed with the scored figures on the same report (ADR-042).

    "Latest" is insertion order, which is the order the tiers ran. ``eval.harness`` uses
    the same rule when it scores exceptions, so the two cannot drift apart.

    Ordering is by rupee at risk rather than by row order, because that is the only
    ordering that respects what an associate's twenty minutes are worth.
    """
    rows = conn.execute(
        text(
            "SELECT exception_id, bank_txn_id, settlement_id, reason_code, "
            "value_at_risk_paise, detail_json FROM exceptions "
            "WHERE run_id = :run AND resolved_at IS NULL "
            "ORDER BY created_at, rowid"
        ),
        {"run": run_id},
    ).all()

    grouped: dict[str, list[Any]] = {}
    for row in rows:
        # An exception that names no credit has nothing to be grouped with.
        key = row.bank_txn_id if row.bank_txn_id is not None else f"id:{row.exception_id}"
        grouped.setdefault(key, []).append(row)

    items: list[QueueItem] = []
    for group in grouped.values():
        latest = group[-1]
        code = ExceptionCode(latest.reason_code)
        detail = json.loads(latest.detail_json or "{}")
        items.append(
            QueueItem(
                exception_id=latest.exception_id,
                bank_txn_id=latest.bank_txn_id,
                settlement_id=latest.settlement_id,
                code=code,
                value_at_risk_paise=latest.value_at_risk_paise,
                detail=detail,
                suggested_action=suggested_action(code, detail),
                history=tuple(ExceptionCode(row.reason_code) for row in group[:-1]),
            )
        )

    return sorted(
        items,
        key=lambda item: (-item.value_at_risk_paise, item.bank_txn_id or "", item.exception_id),
    )


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
        actions = {item.suggested_action for item in members}
        action = actions.pop() if len(actions) == 1 else SUGGESTED_ACTION[code]
        return f"{len(members)} record(s). {action}"

    if code is ExceptionCode.AMOUNT_BEYOND_TOLERANCE:
        from_model = sum(
            1 for item in members if item.detail.get("raised_by") == RAISED_BY_TIER3
        )
        if from_model == len(members):
            return (
                f"{len(members)} model proposals named settlements that do not add up to "
                "the credit, and Python refused every one. That is the arithmetic gate "
                "working, not a sign the fee model is wrong — each credit still needs its "
                "real settlements found."
            )
        fee_model = (
            f"{len(members)} records share this code. That is not {len(members)} problems "
            "— it is most likely one wrong assumption in the fee model for this merchant. "
            "Correcting the model resolves the whole class at once."
        )
        if from_model:
            fee_model += (
                f" {from_model} of them are model proposals the arithmetic gate refused, "
                "which do not add up for a different reason."
            )
        return fee_model
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
