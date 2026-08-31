"""Prompt version 1. The version string goes into every tier-3 provenance record.

The prompt states plainly that the model may only choose from the supplied candidate IDs
or return NO_MATCH, and that it must not compute or adjust amounts.

Saying so is not what enforces it — `cascade/gates.py` does that, and the gates would
reject a fabricated ID whether or not the prompt asked nicely. The instruction is here so
that a model which *can* comply is told how, and so the audit trail records what it was
asked. A prompt that says "you may not invent identifiers" and a membership gate that
rejects them are doing different jobs.

**Never edit a shipped prompt in place — add v2.** A prompt change that is invisible in
the audit trail makes every past tier-3 match unexplainable: the record would name a
version whose text no longer exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ledgerloop.generate.fee_model import FeeModel
from ledgerloop.ingest.schemas import BankRow, SettlementRow

PROMPT_VERSION = "v1"

_TEMPLATE = """You are reconciling one bank credit against a list of candidate gateway \
settlements for an Indian merchant.

THE BANK CREDIT
  transaction id : {bank_txn_id}
  value date     : {value_date}
  amount credited: {credit_paise} paise
  narration      : {narration}

The narration is free text written by the bank. Any reference it contains may be \
prefixed, truncated, case-varied or absent entirely.

HOW THE AMOUNTS RELATE
  net = gross - MDR fee - GST on the fee - TDS
A credit may also cover several settlements at once, in which case it equals the sum of \
their net amounts. A credit normally lands on the settlement date or within {slack} \
business days after it.

CANDIDATE SETTLEMENTS
{candidates}

YOUR TASK
Decide which of the candidates above, if any, this credit pays for.

RULES
1. You may only name settlement ids from the list above. Never invent one. If none fit, \
return NO_MATCH with a reason.
2. Do not compute, adjust or reconcile amounts. They are recomputed independently in \
Python and that recomputation decides; your answer is evidence, not a verdict.
3. If two different sets of settlements would both explain this credit, return NO_MATCH \
and say so. Ambiguity is resolved by a human, never by you.
4. Give per-field evidence for every correspondence you claim.

Reply with JSON only, matching this shape:
{{"decision": "MATCH" | "NO_MATCH",
  "matched_settlement_ids": ["..."],
  "evidence": [{{"field_name": "...", "bank_value": "...", "settlement_value": "...", \
"reasoning": "under 200 chars"}}],
  "confidence": 0.0 to 1.0,
  "unresolved_reason": "required when NO_MATCH"}}"""


def render(
    bank_txn: BankRow,
    candidates: Sequence[SettlementRow],
    *,
    fee_model: FeeModel,
    slack_days: int,
) -> str:
    """Render the adjudication prompt for one credit.

    Deterministic by construction: the caller supplies candidates in a stable order and
    nothing here introduces variation. §7.4's promise that a re-run makes zero API calls
    depends on this producing identical bytes for identical inputs.
    """
    del fee_model  # rates are described in prose; the model must not compute with them
    lines = []
    for row in candidates:
        lines.append(
            f"  {row.settlement_id}"
            f" | net {row.net_amount_paise} paise"
            f" | settled {row.settled_on.isoformat()}"
            f" | customer {row.customer_name}"
            f" | reference {row.utr or '(none recorded)'}"
            f" | status {row.status}"
        )

    return _TEMPLATE.format(
        bank_txn_id=bank_txn.bank_txn_id,
        value_date=bank_txn.value_date.isoformat(),
        credit_paise=bank_txn.credit_paise,
        narration=bank_txn.narration,
        slack=slack_days,
        candidates="\n".join(lines),
    )


def earliest_settlement(candidates: Sequence[SettlementRow]) -> date | None:
    """Earliest settlement date in the candidate set, for window reporting."""
    return min((row.settled_on for row in candidates), default=None)
