"""The three gates.

Every Tier 3 proposal passes all three before any match is posted. This module is
the architectural thesis of the whole project in executable form:

    1. SCHEMA      — does the response parse against the contract?
    2. MEMBERSHIP  — are all IDs from the candidate set we actually supplied?
    3. ARITHMETIC  — do the numbers work when recomputed in Python?

If the model proposes a pairing whose amounts do not reconcile, **the numbers win**.
The model never overrides arithmetic. It is proposing evidence, not deciding.

The membership gate is also the hallucination detector. Its rejection count is a
reported metric — proof that the gate fires on real data rather than being a claim
in a slide.

There is a fourth check, the confidence threshold, which is deliberately *not* called a
gate: it is a dial that trades auto-match rate against false-match rate, not a
correctness boundary. Say "three gates and a threshold" rather than "four gates", and
the pitch stays accurate when someone counts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from pydantic import ValidationError

from ledgerloop.config import DEFAULT_MATCH_CONFIG, MatchConfig
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, FeeModel
from ledgerloop.ingest.schemas import SettlementRow
from ledgerloop.llm.contract import CONFIDENCE_THRESHOLD, Adjudication
from ledgerloop.money import within_tolerance

#: Providers wrap JSON in code fences unprompted. Rejecting an otherwise valid response
#: over formatting would attribute a provider quirk to the model's judgement.
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


@dataclass(frozen=True)
class GateResult:
    """Outcome of running a proposal through the gates."""

    accepted: bool
    adjudication: Adjudication | None = None
    exception_code: ExceptionCode | None = None
    detail: str = ""
    hallucinated_ids: tuple[str, ...] = ()


def schema_gate(raw_response: str) -> GateResult:
    """Gate 1 — parse and validate against the contract.

    There is deliberately no retry, no reprompt and no repair. A model that emitted
    invalid JSON for this input had nothing confident to say about it; coaxing it into
    valid JSON manufactures a guess and dresses it as an answer.
    """
    try:
        adjudication = Adjudication.model_validate_json(_FENCE.sub("", raw_response))
    except ValidationError as error:
        return GateResult(
            accepted=False,
            exception_code=ExceptionCode.LLM_INVALID_OUTPUT,
            detail=f"response did not validate against the contract: {error}",
        )
    return GateResult(accepted=True, adjudication=adjudication)


def membership_gate(
    adjudication: Adjudication,
    candidate_ids: frozenset[str],
) -> GateResult:
    """Gate 2 — every proposed ID must come from the candidate set we supplied.

    Any ID outside ``candidate_ids`` is a hallucination, and the **entire** response is
    discarded — not just the offending ID.

    Partial acceptance is tempting and wrong: a response containing a fabricated ID is
    evidence that the model was pattern-completing rather than reading, which devalues
    the IDs that happened to be real.

    Every fabricated ID is reported, not only the first. The count is a published
    metric, and reporting one of three would understate it.
    """
    fabricated = tuple(
        sorted(
            identifier
            for identifier in adjudication.matched_settlement_ids
            if identifier not in candidate_ids
        )
    )
    if fabricated:
        return GateResult(
            accepted=False,
            exception_code=ExceptionCode.LLM_INVALID_OUTPUT,
            detail=(
                f"response named {len(fabricated)} identifier(s) outside the candidate "
                "set supplied to the model"
            ),
            hallucinated_ids=fabricated,
        )
    return GateResult(accepted=True, adjudication=adjudication)


def arithmetic_gate(
    adjudication: Adjudication,
    bank_credit_paise: int,
    bank_value_date: date,
    settlements_by_id: dict[str, SettlementRow],
    *,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
) -> GateResult:
    """Gate 3 — recompute the match in Python; the numbers are authoritative.

    However good the reasoning reads and however high the confidence, a proposal whose
    amounts do not reconcile does not post. This gate is why a hallucinated-but-plausible
    match cannot reach the ledger.

    The tolerance bands are the **same** ones Tier 1 uses. A stricter gate here would
    reject records Tier 1 would have accepted had it seen them, turning the tier into a
    filter on its own residual rather than an adjudicator of it.
    """
    if adjudication.decision == "NO_MATCH":
        return GateResult(accepted=True, adjudication=adjudication)

    members = [settlements_by_id[i] for i in adjudication.matched_settlement_ids]
    total = sum(row.net_amount_paise for row in members)

    if not within_tolerance(
        bank_credit_paise,
        total,
        abs_paise=config.amount_tolerance_paise,
        rel_bps=config.amount_tolerance_bps,
    ):
        return GateResult(
            accepted=False,
            exception_code=ExceptionCode.AMOUNT_BEYOND_TOLERANCE,
            detail=(
                f"proposed settlements sum to {total} paise against a credit of "
                f"{bank_credit_paise}; the numbers decide, not the model"
            ),
        )

    earliest = min(row.settled_on for row in members)
    latest = fee_model.business_days_after(earliest, config.settlement_slack_days)
    if not earliest <= bank_value_date <= latest:
        return GateResult(
            accepted=False,
            exception_code=ExceptionCode.DATE_OUT_OF_WINDOW,
            detail=(
                f"credit dated {bank_value_date.isoformat()} falls outside "
                f"{earliest.isoformat()}..{latest.isoformat()}"
            ),
        )

    return GateResult(accepted=True, adjudication=adjudication)


def confidence_gate(adjudication: Adjudication) -> GateResult:
    """The acceptance threshold — a dial, not a correctness boundary.

    A low score is the model being candid rather than dishonest, so the record becomes a
    queue item a human can look at instead of a silent discard.
    """
    if adjudication.decision == "MATCH" and adjudication.confidence < CONFIDENCE_THRESHOLD:
        return GateResult(
            accepted=False,
            exception_code=ExceptionCode.LOW_CONFIDENCE,
            detail=(
                f"model reported {adjudication.confidence:.2f}, below the "
                f"{CONFIDENCE_THRESHOLD:.2f} acceptance threshold"
            ),
        )
    return GateResult(accepted=True, adjudication=adjudication)


def run_all_gates(
    raw_response: str,
    candidate_ids: frozenset[str],
    bank_credit_paise: int,
    bank_value_date: date,
    settlements_by_id: dict[str, SettlementRow],
    *,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
) -> GateResult:
    """Run the gates in order, short-circuiting on the first rejection.

    Order matters for two reasons. Looking up a fabricated ID is itself a bug surface —
    and worse, a hallucinated ID that happened to collide with a real settlement would
    otherwise be arithmetically validated and look entirely legitimate.
    """
    parsed = schema_gate(raw_response)
    if not parsed.accepted or parsed.adjudication is None:
        return parsed

    checked = membership_gate(parsed.adjudication, candidate_ids)
    if not checked.accepted:
        return checked

    computed = arithmetic_gate(
        parsed.adjudication,
        bank_credit_paise,
        bank_value_date,
        settlements_by_id,
        fee_model=fee_model,
        config=config,
    )
    if not computed.accepted:
        return computed

    return confidence_gate(parsed.adjudication)
