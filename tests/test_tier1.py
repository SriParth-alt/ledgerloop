"""Tier 1 — tolerant deterministic matching. Still no LLM.

Tier 1 is where the fee model finally earns its property tests: the amount a bank
actually credited is `gross - MDR - GST - TDS`, and this tier is the first thing that
has to reproduce that arithmetic to recognise a payment.

Three things are load-bearing here and each gets more tests than the happy path.

**Amount and date are gates, not weights.** §6 describes a weighted composite, which
would let a match clear the threshold on name and reference alone while the money does
not reconcile. That contradicts ADR-002 — arithmetic decides. Scoring only ever ranks
candidates that already reconcile.

**A tie is declined.** When two settlements score identically for one credit the
ordering is a tiebreak we invented, and picking one is the coin flip ADR-003 forbids.

**The weights are derived, not tuned.** Each contribution is set so that the chaos
injector §5.5 assigns to Tier 1 lands in Tier 1, and the one it assigns to Tier 3 does
not. The table in `test_score_reproduces_the_tier_assignments_in_the_spec` is the whole
justification, and it is the thing to defend if anyone asks where 0.70 came from.
"""

from __future__ import annotations

from datetime import date

from ledgerloop.audit.provenance import ProposedMatch
from ledgerloop.cascade.tier1_tolerant import (
    RULE_TOLERANT,
    amount_agrees,
    date_in_window,
    expected_net_paise,
    match_tier1,
    name_similarity,
    reference_distance,
    score_candidate,
)
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, PaymentMethod
from ledgerloop.ingest.schemas import BankRow, SettlementRow

DAY = date(2026, 8, 10)
GROSS = 100_000
UTR = "RZRPY1234567"
CUSTOMER = "ACME RETAIL PVT LTD"

#: gross 100000 - fee 2000 - gst 360 - tds 100. Hardcoded rather than recomputed, so a
#: change to the fee model breaks this loudly instead of the test moving with it.
EXPECTED_NET = 97_540


def bank(
    txn_id: str = "BNK1",
    *,
    narration: str = f"NEFT-CR/HDFC/{UTR}/{CUSTOMER}/BLR",
    credit: int = EXPECTED_NET,
    value_date: date = DAY,
) -> BankRow:
    return BankRow(
        bank_txn_id=txn_id,
        value_date=value_date,
        narration=narration,
        credit_paise=credit,
        debit_paise=0,
        balance_paise=credit,
    )


def settlement(
    settlement_id: str = "STL1",
    *,
    utr: str | None = UTR,
    gross: int = GROSS,
    settled_on: date = DAY,
    method: PaymentMethod = PaymentMethod.CREDIT_CARD,
) -> SettlementRow:
    breakdown = SETTLEMENT_FEE_MODEL.breakdown(gross, method)
    return SettlementRow(
        settlement_id=settlement_id,
        payment_id=f"PAY{settlement_id}",
        order_id=f"ORD{settlement_id}",
        invoice_ref=f"INV{settlement_id}",
        customer_name=CUSTOMER,
        method=method,
        gross_amount_paise=gross,
        fee_paise=breakdown["fee_paise"],
        gst_on_fee_paise=breakdown["gst_on_fee_paise"],
        tds_paise=breakdown["tds_paise"],
        net_amount_paise=breakdown["net_paise"],
        captured_at=DAY,
        settled_on=settled_on,
        utr=utr,
        status="captured",
    )


def _score(bank_row: BankRow, settlement_row: SettlementRow) -> float | None:
    result = score_candidate(
        bank_row,
        settlement_row,
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )
    return None if result is None else round(result.score, 4)


# --- the arithmetic -------------------------------------------------------------


def test_expected_net_reproduces_the_fee_model_exactly() -> None:
    """Generator and matcher must share one fee model. If Tier 1 rounds even one paise
    differently it will appear to work on synthetic data for the wrong reason."""
    assert expected_net_paise(settlement(), fee_model=SETTLEMENT_FEE_MODEL) == EXPECTED_NET


def test_amount_agrees_inside_the_tolerance_band() -> None:
    """PAISE_DRIFT is ±1-3 paise; the band is max(₹1, 0.5%), so drift must survive."""
    for drift in (-3, -1, 0, 1, 3):
        assert amount_agrees(
            EXPECTED_NET + drift,
            settlement(),
            fee_model=SETTLEMENT_FEE_MODEL,
            config=DEFAULT_MATCH_CONFIG,
        )


def test_amount_disagrees_outside_the_tolerance_band() -> None:
    """0.5% of 97,540 paise is 488. A rupee out is a different payment, not drift."""
    assert not amount_agrees(
        EXPECTED_NET + 1_000,
        settlement(),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )


# --- the date window ------------------------------------------------------------


def test_date_window_opens_on_the_settlement_date() -> None:
    """The window anchors on `settled_on` — what the gateway reported — rather than on
    a lag recomputed from `captured_at`, which would assume the answer."""
    assert date_in_window(
        bank(value_date=DAY),
        settlement(settled_on=DAY),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )


def test_date_window_extends_by_business_days_not_calendar_days() -> None:
    """Weekends and holidays are skipped, so the window is wider in wall-clock terms
    than it looks. Inline date math is how a window ends up right in August and wrong
    in October."""
    latest = SETTLEMENT_FEE_MODEL.business_days_after(
        DAY, DEFAULT_MATCH_CONFIG.settlement_slack_days
    )

    assert date_in_window(
        bank(value_date=latest),
        settlement(settled_on=DAY),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )


def test_date_outside_the_window_is_rejected() -> None:
    beyond = SETTLEMENT_FEE_MODEL.business_days_after(
        DAY, DEFAULT_MATCH_CONFIG.settlement_slack_days + 1
    )

    assert not date_in_window(
        bank(value_date=beyond),
        settlement(settled_on=DAY),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )


def test_a_credit_dated_before_settlement_is_rejected() -> None:
    """Money cannot arrive before the gateway released it. A credit earlier than
    `settled_on` is evidence of a different payment, not of an early one."""
    assert not date_in_window(
        bank(value_date=date(2026, 8, 7)),
        settlement(settled_on=DAY),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )


# --- the fuzzy reference --------------------------------------------------------


def test_reference_distance_is_zero_for_an_intact_token() -> None:
    assert reference_distance(f"NEFT-CR/HDFC/{UTR}/ACME/BLR", UTR) == 0


def test_reference_distance_sees_through_a_truncation() -> None:
    """NARRATION_NOISE truncates by at most two characters, which is exactly what
    `fuzzy_utr_max_distance` is sized for."""
    assert reference_distance("NEFT-CR/HDFC/RZRPY12345/ACME/BLR", UTR) == 2


def test_reference_distance_is_none_when_the_narration_carries_no_token() -> None:
    """NO_UTR chaos. Absent evidence must read as absent, not as a distant match."""
    assert reference_distance("NEFT-CR/HDFC/ACME RETAIL PVT/BLR", UTR) is None


def test_reference_distance_is_none_when_the_settlement_has_no_reference() -> None:
    assert reference_distance(f"NEFT-CR/HDFC/{UTR}/ACME/BLR", None) is None


# --- the name signal ------------------------------------------------------------


def test_name_similarity_recognises_the_counterparty() -> None:
    assert (
        name_similarity(CUSTOMER, f"NEFT-CR/HDFC/{UTR}/{CUSTOMER}/BLR")
        >= DEFAULT_MATCH_CONFIG.name_similarity_threshold
    )


def test_name_similarity_rejects_a_different_company() -> None:
    assert (
        name_similarity(CUSTOMER, f"NEFT-CR/HDFC/{UTR}/NIMBUS TEXTILES LTD/BLR")
        < DEFAULT_MATCH_CONFIG.name_similarity_threshold
    )


# --- gates before scoring -------------------------------------------------------


def test_a_wrong_amount_cannot_be_rescued_by_perfect_evidence() -> None:
    """The gate, stated as bluntly as possible. Reference and name are both perfect
    here; the money is not. ADR-002 says arithmetic decides, so this must not match —
    it would be exactly the behaviour rule 2 forbids the LLM, done by a rule instead.
    """
    assert _score(bank(credit=EXPECTED_NET + 5_000), settlement()) is None


def test_a_date_outside_the_window_cannot_be_rescued_by_perfect_evidence() -> None:
    beyond = SETTLEMENT_FEE_MODEL.business_days_after(
        DAY, DEFAULT_MATCH_CONFIG.settlement_slack_days + 2
    )

    assert _score(bank(value_date=beyond), settlement()) is None


# --- the composite score --------------------------------------------------------


def test_score_reproduces_the_tier_assignments_in_the_spec() -> None:
    """The justification for the weights, in one table.

    They are not tuned. Each contribution is set so the injector §5.5 assigns to Tier 1
    lands in Tier 1, and the one it assigns to Tier 3 falls through to Tier 3.
    """
    config = DEFAULT_MATCH_CONFIG
    gates = config.tier1_score_amount_and_date
    with_reference = gates + config.tier1_score_fuzzy_reference
    with_name = gates + config.tier1_score_name

    # amount + date + fuzzy reference — NARRATION_NOISE belongs here
    assert _score(bank(narration=f"NEFT-CR/HDFC/RZRPY12345/{CUSTOMER}/BLR"), settlement()) == round(
        with_reference + config.tier1_score_name, 4
    )
    # amount + date + name only — NO_UTR must fall through to Tier 3
    assert _score(bank(narration=f"NEFT-CR/HDFC/{CUSTOMER}/BLR"), settlement()) == round(
        with_name, 4
    )
    # amount + date alone
    assert _score(bank(narration="NEFT-CR/HDFC/UNRELATED PAYER/BLR"), settlement()) == round(
        gates, 4
    )

    assert with_name < config.tier1_auto_post_confidence <= with_reference


def test_no_utr_rows_fall_through_rather_than_posting() -> None:
    """§5.5 assigns NO_UTR to Tier 3. If Tier 1 posted these, the ablation would
    attribute Tier 3's contribution to the wrong tier and the LLM invocation rate —
    the number ADR-002 rests on — would be measured against the wrong denominator.
    """
    matches = match_tier1(
        [bank(narration=f"NEFT-CR/HDFC/{CUSTOMER}/BLR")],
        [settlement()],
    )

    assert matches == []


def test_a_truncated_reference_posts() -> None:
    matches = match_tier1(
        [bank(narration=f"NEFT-CR/HDFC/RZRPY12345/{CUSTOMER}/BLR")], [settlement()]
    )

    assert len(matches) == 1
    assert matches[0].tier == 1
    assert matches[0].rule_id == RULE_TOLERANT
    assert matches[0].settlement_ids == ("STL1",)
    assert matches[0].confidence >= DEFAULT_MATCH_CONFIG.tier1_auto_post_confidence
    assert matches[0].confidence < 1.0, "confidence 1.0 belongs to Tier 0 alone"


def test_paise_drift_still_posts() -> None:
    matches = match_tier1(
        [
            bank(
                narration=f"NEFT-CR/HDFC/RZRPY12345/{CUSTOMER}/BLR",
                credit=EXPECTED_NET - 3,
            )
        ],
        [settlement()],
    )

    assert len(matches) == 1


# --- ambiguity ------------------------------------------------------------------


def test_two_identically_scoring_settlements_produce_no_match() -> None:
    """ADR-003 applied to Tier 1. Both candidates reconcile, both carry the same
    evidence, and the ranking between them is something we invented. Falling through
    is silent rather than raising AMBIGUOUS_SUBSET, because that code means subset
    ambiguity and Tier 2 may yet explain the credit as a batch.
    """
    matches = match_tier1(
        [bank(narration=f"NEFT-CR/HDFC/RZRPY12345/{CUSTOMER}/BLR")],
        [settlement("STL1"), settlement("STL2")],
    )

    assert matches == []


def test_a_clearly_better_candidate_still_wins() -> None:
    """Declining every contested credit would be over-correction. Only an exact tie is
    a coin flip; a strictly better candidate is a decision the evidence supports."""
    stronger = settlement("STL1")
    weaker = settlement("STL2", utr="RZRPY9999999")

    matches = match_tier1(
        [bank(narration=f"NEFT-CR/HDFC/{UTR}/{CUSTOMER}/BLR")], [stronger, weaker]
    )

    assert len(matches) == 1
    assert matches[0].settlement_ids == ("STL1",)


def test_a_settlement_is_never_posted_twice() -> None:
    matches = match_tier1(
        [
            bank("BNK1", narration=f"NEFT-CR/HDFC/{UTR}/{CUSTOMER}/BLR"),
            bank("BNK2", narration=f"NEFT-CR/HDFC/{UTR}/{CUSTOMER}/BLR"),
        ],
        [settlement()],
    )
    claimed = [sid for match in matches for sid in match.settlement_ids]

    assert len(claimed) == len(set(claimed))


# --- evidence -------------------------------------------------------------------


def test_posted_match_records_which_signals_fired() -> None:
    """A human overturning a Tier 1 match needs to see *why* it was made — which is
    also what turns a cluster of wrong matches into one diagnosable wrong assumption.
    """
    matches: list[ProposedMatch] = match_tier1(
        [bank(narration=f"NEFT-CR/HDFC/RZRPY12345/{CUSTOMER}/BLR")], [settlement()]
    )
    fields = {item.field for item in matches[0].evidence}

    assert {"expected_net_paise", "value_date", "narration_token"} <= fields
