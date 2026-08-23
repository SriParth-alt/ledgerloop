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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.llm.contract import Adjudication


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

    TODO(day-9): implement.

    Strip any markdown fences the provider adds, then validate with
    ``Adjudication.model_validate_json``. On ``ValidationError`` return a rejecting
    GateResult with ``LLM_INVALID_OUTPUT``.

    Do NOT retry, reprompt, or attempt repair. A model that emitted invalid JSON for
    this input had nothing confident to say about it; coaxing it into valid JSON
    manufactures a guess.
    """
    raise NotImplementedError


def membership_gate(
    adjudication: Adjudication,
    candidate_ids: frozenset[str],
) -> GateResult:
    """Gate 2 — every proposed ID must come from the candidate set we supplied.

    TODO(day-9): implement.

    Any ID outside ``candidate_ids`` is a hallucination. Discard the *entire*
    response — not just the offending ID — and return ``LLM_INVALID_OUTPUT`` with
    the offending IDs in ``hallucinated_ids`` so the run metrics can count them.

    Partial acceptance is tempting and wrong: a response containing a fabricated ID
    is evidence that the model was pattern-completing rather than reading, which
    devalues the IDs that happened to be real.
    """
    raise NotImplementedError


def arithmetic_gate(
    adjudication: Adjudication,
    bank_credit_paise: int,
    bank_value_date: date,
    settlements_by_id: dict[str, object],
) -> GateResult:
    """Gate 3 — recompute the match in Python; the numbers are authoritative.

    TODO(day-9): implement.

    Sum ``net_paise`` across the proposed settlements and compare to
    ``bank_credit_paise`` using ``money.within_tolerance`` with the same bands Tier 1
    uses. Then confirm ``bank_value_date`` falls inside
    ``FeeModel.settlement_window`` for the earliest capture in the set.

    Amount failure  -> AMOUNT_BEYOND_TOLERANCE
    Date failure    -> DATE_OUT_OF_WINDOW

    This gate is why a hallucinated-but-plausible match cannot reach the ledger.
    """
    raise NotImplementedError


def confidence_gate(adjudication: Adjudication) -> GateResult:
    """Gate 3.5 — the acceptance threshold.

    TODO(day-9): implement. Below ``CONFIDENCE_THRESHOLD`` becomes a
    ``LOW_CONFIDENCE`` exception rather than a match.
    """
    raise NotImplementedError


def run_all_gates(
    raw_response: str,
    candidate_ids: frozenset[str],
    bank_credit_paise: int,
    bank_value_date: date,
    settlements_by_id: dict[str, object],
) -> GateResult:
    """Run gates in order, short-circuiting on the first rejection.

    TODO(day-9): implement. Order matters — never run arithmetic on IDs that failed
    membership, because looking up a fabricated ID is itself a bug surface.
    """
    raise NotImplementedError
