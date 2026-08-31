"""The three gates — the architectural thesis in executable form.

Every Tier 3 proposal passes all of them before any money moves. Most of this file is
about the gates *rejecting* things, because that is what they are for: the model is a
proposer of evidence, and these are the checks that stop a proposal becoming a decision.

Four properties are load-bearing, and each is easy to get subtly wrong:

**No retry.** A model that emitted invalid JSON for this input had nothing confident to
say about it. Reprompting until the output parses manufactures a guess and calls it an
answer.

**Whole-response discard.** A response containing one fabricated id is evidence the model
was pattern-completing rather than reading, which devalues the ids that happened to be
real. Partial acceptance is the tempting, wrong version.

**The numbers win.** A proposal whose amounts do not reconcile is rejected however good
its reasoning reads. This is the gate that makes ADR-002's claim true rather than stated.

**Order matters.** Membership runs before arithmetic, because looking up a fabricated id
is itself a bug surface — and because a hallucinated id that happened to collide with a
real settlement would otherwise get arithmetically validated and look legitimate.
"""

from __future__ import annotations

import json
from datetime import date

from ledgerloop.cascade.gates import (
    arithmetic_gate,
    confidence_gate,
    membership_gate,
    run_all_gates,
    schema_gate,
)
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import PaymentMethod
from ledgerloop.ingest.schemas import SettlementRow
from ledgerloop.llm.contract import CONFIDENCE_THRESHOLD, Adjudication

DAY = date(2026, 8, 10)


def settlement(settlement_id: str, *, net: int, settled_on: date = DAY) -> SettlementRow:
    return SettlementRow(
        settlement_id=settlement_id,
        payment_id=f"PAY{settlement_id}",
        order_id=f"ORD{settlement_id}",
        invoice_ref=f"INV{settlement_id}",
        customer_name="ACME RETAIL PVT LTD",
        method=PaymentMethod.UPI,
        gross_amount_paise=net,
        fee_paise=0,
        gst_on_fee_paise=0,
        tds_paise=0,
        net_amount_paise=net,
        captured_at=DAY,
        settled_on=settled_on,
        utr=f"RZRPY{settlement_id[-7:].rjust(7, '0')}",
        status="captured",
    )


def response(
    *,
    decision: str = "MATCH",
    ids: list[str] | None = None,
    confidence: float = 0.95,
    reason: str | None = None,
) -> str:
    """A well-formed model response. Tests mutate one field at a time from here."""
    body: dict[str, object] = {
        "decision": decision,
        "matched_settlement_ids": ids if ids is not None else ["STL1"],
        "confidence": confidence,
    }
    if decision == "MATCH":
        body["evidence"] = [
            {
                "field_name": "narration_token",
                "bank_value": "RZRPY0000001",
                "settlement_value": "RZRPY0000001",
                "reasoning": "reference appears in the narration",
            }
        ]
    else:
        body["matched_settlement_ids"] = []
        body["evidence"] = []
        body["unresolved_reason"] = reason or "no candidate explains this credit"
    return json.dumps(body)


def _adjudication(**kwargs: object) -> Adjudication:
    return Adjudication.model_validate_json(response(**kwargs))  # type: ignore[arg-type]


# =================================================================================
# Gate 1 — schema
# =================================================================================


def test_a_well_formed_response_passes_the_schema_gate() -> None:
    result = schema_gate(response())

    assert result.accepted
    assert result.adjudication is not None
    assert result.adjudication.decision == "MATCH"


def test_malformed_json_is_rejected_without_retry() -> None:
    """§7.3. There is deliberately no retry loop here — retrying until the output
    parses is how you coax a guess out of a model that had nothing to say."""
    result = schema_gate("{not json at all")

    assert not result.accepted
    assert result.exception_code is ExceptionCode.LLM_INVALID_OUTPUT


def test_a_response_violating_the_contract_is_rejected() -> None:
    """The contract's own validator: MATCH with no ids means the model did not
    understand the task. Interpreting intent is not this gate's job."""
    result = schema_gate(
        json.dumps({"decision": "MATCH", "matched_settlement_ids": [], "confidence": 0.9})
    )

    assert not result.accepted
    assert result.exception_code is ExceptionCode.LLM_INVALID_OUTPUT


def test_markdown_fences_are_stripped_before_parsing() -> None:
    """Providers wrap JSON in code fences unprompted. Rejecting an otherwise valid
    response over formatting would attribute a provider quirk to the model's judgement.
    """
    result = schema_gate(f"```json\n{response()}\n```")

    assert result.accepted


# =================================================================================
# Gate 2 — membership, and the hallucination counter
# =================================================================================


def test_ids_from_the_candidate_set_pass() -> None:
    result = membership_gate(_adjudication(ids=["STL1"]), frozenset({"STL1", "STL2"}))

    assert result.accepted


def test_a_fabricated_id_discards_the_entire_response() -> None:
    """§7.3, and the tempting wrong version is partial acceptance.

    A response naming one real id and one invented one is evidence the model was
    pattern-completing rather than reading the candidates it was given. That devalues
    the id that happened to be real, so the whole response goes.
    """
    result = membership_gate(
        _adjudication(ids=["STL1", "STL_INVENTED"]), frozenset({"STL1", "STL2"})
    )

    assert not result.accepted
    assert result.exception_code is ExceptionCode.LLM_INVALID_OUTPUT
    assert result.hallucinated_ids == ("STL_INVENTED",)
    assert result.adjudication is None, "a rejected response must not be carried forward"


def test_every_hallucinated_id_is_reported_not_just_the_first() -> None:
    """The count is a published metric — §9.1's proof that the gate fires on real data.
    Reporting one of three would understate it."""
    result = membership_gate(
        _adjudication(ids=["GHOST1", "GHOST2"]), frozenset({"STL1"})
    )

    assert set(result.hallucinated_ids) == {"GHOST1", "GHOST2"}


def test_a_no_match_response_passes_membership_trivially() -> None:
    """NO_MATCH names no ids, so there is nothing to fabricate."""
    result = membership_gate(_adjudication(decision="NO_MATCH"), frozenset({"STL1"}))

    assert result.accepted


# =================================================================================
# Gate 3 — arithmetic. The numbers win.
# =================================================================================


def test_a_reconciling_proposal_passes() -> None:
    result = arithmetic_gate(
        _adjudication(ids=["STL1", "STL2"]),
        bank_credit_paise=500,
        bank_value_date=DAY,
        settlements_by_id={
            "STL1": settlement("STL1", net=300),
            "STL2": settlement("STL2", net=200),
        },
    )

    assert result.accepted


def test_a_proposal_whose_amounts_do_not_reconcile_is_rejected() -> None:
    """The gate that makes ADR-002 true rather than merely stated.

    The model's reasoning here is irrelevant. If the numbers disagree with the fee
    model, the numbers win — this is the same rule the deterministic tiers follow, and
    the model is not exempt from it.
    """
    result = arithmetic_gate(
        _adjudication(ids=["STL1"]),
        bank_credit_paise=999_999,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert not result.accepted
    assert result.exception_code is ExceptionCode.AMOUNT_BEYOND_TOLERANCE


def test_a_proposal_outside_the_settlement_window_is_rejected() -> None:
    result = arithmetic_gate(
        _adjudication(ids=["STL1"]),
        bank_credit_paise=300,
        bank_value_date=date(2026, 9, 30),
        settlements_by_id={"STL1": settlement("STL1", net=300, settled_on=DAY)},
    )

    assert not result.accepted
    assert result.exception_code is ExceptionCode.DATE_OUT_OF_WINDOW


def test_paise_drift_is_tolerated_by_the_same_band_tier_one_uses() -> None:
    """The gate must not be stricter than the deterministic tiers, or Tier 3 would
    reject records Tier 1 would have accepted had it seen them."""
    result = arithmetic_gate(
        _adjudication(ids=["STL1"]),
        bank_credit_paise=298,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert result.accepted


# =================================================================================
# The confidence threshold
# =================================================================================


def test_a_confident_proposal_passes() -> None:
    assert confidence_gate(_adjudication(confidence=CONFIDENCE_THRESHOLD)).accepted


def test_a_proposal_below_the_threshold_becomes_an_exception() -> None:
    """Not a rejection of the model's honesty — a low score is the model being candid.
    It becomes a queue item a human can look at, not a silent discard."""
    result = confidence_gate(_adjudication(confidence=CONFIDENCE_THRESHOLD - 0.01))

    assert not result.accepted
    assert result.exception_code is ExceptionCode.LOW_CONFIDENCE


# =================================================================================
# Order
# =================================================================================


def test_gates_run_in_order_and_stop_at_the_first_failure() -> None:
    """A response that fails both membership and arithmetic must report *membership*.

    The order is not cosmetic. Looking up a fabricated id is a bug surface in itself,
    and worse — a hallucinated id that happened to collide with a real settlement would
    otherwise be arithmetically validated and look legitimate.
    """
    result = run_all_gates(
        response(ids=["STL_INVENTED"]),
        candidate_ids=frozenset({"STL1"}),
        bank_credit_paise=999_999,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert not result.accepted
    assert result.exception_code is ExceptionCode.LLM_INVALID_OUTPUT
    assert result.hallucinated_ids == ("STL_INVENTED",)


def test_malformed_json_never_reaches_membership() -> None:
    result = run_all_gates(
        "not json",
        candidate_ids=frozenset({"STL1"}),
        bank_credit_paise=300,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert result.exception_code is ExceptionCode.LLM_INVALID_OUTPUT
    assert result.hallucinated_ids == (), "nothing was parsed, so nothing was hallucinated"


def test_a_proposal_passing_every_gate_is_accepted() -> None:
    result = run_all_gates(
        response(ids=["STL1"]),
        candidate_ids=frozenset({"STL1", "STL2"}),
        bank_credit_paise=300,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert result.accepted
    assert result.adjudication is not None
    assert result.adjudication.matched_settlement_ids == ["STL1"]


def test_a_valid_no_match_passes_the_gates_without_proposing_anything() -> None:
    """The model declining is a legitimate outcome, distinct from the model failing."""
    result = run_all_gates(
        response(decision="NO_MATCH", reason="narration names a different counterparty"),
        candidate_ids=frozenset({"STL1"}),
        bank_credit_paise=300,
        bank_value_date=DAY,
        settlements_by_id={"STL1": settlement("STL1", net=300)},
    )

    assert result.accepted
    assert result.adjudication is not None
    assert result.adjudication.decision == "NO_MATCH"
