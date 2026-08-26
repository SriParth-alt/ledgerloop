"""Tier 0 — exact deterministic matching.

Tier 0 exists to be unimpeachable. Everything it posts carries confidence 1.0 and is
never revisited, so a wrong match here is the most expensive kind of bug in the whole
project: it closes silently, at full confidence, with a provenance record asserting
certainty.

That is why the uniqueness guard gets more tests than the matching itself. Most of
this file is about Tier 0 **declining** to match.

Note the imports: this module needs no database and no network, and neither does the
code it tests. Hard rule 1 — tiers 0-2 never call a model — is auditable here by
reading the import block.
"""

from __future__ import annotations

from datetime import date

from ledgerloop.audit.provenance import ProposedMatch
from ledgerloop.cascade.tier0_exact import (
    RULE_AMOUNT_DATE_UNIQUE,
    RULE_UTR_EXACT,
    match_tier0,
    normalise_utr,
    utr_candidates,
)
from ledgerloop.generate.fee_model import PaymentMethod
from ledgerloop.ingest.schemas import BankRow, SettlementRow

DAY = date(2026, 8, 10)
OTHER_DAY = date(2026, 8, 11)


def bank(
    txn_id: str = "BNK1",
    *,
    narration: str = "NEFT-CR/HDFC/RZRPY1234567/ACME RETAIL PVT LTD/BLR",
    credit: int = 100_00,
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
    utr: str | None = "RZRPY1234567",
    net: int = 100_00,
    settled_on: date = DAY,
) -> SettlementRow:
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
        utr=utr,
        status="captured",
    )


def _rules(matches: list[ProposedMatch]) -> list[str]:
    return [match.rule_id for match in matches]


# --- normalisation --------------------------------------------------------------


def test_normalise_uppercases_and_strips_punctuation() -> None:
    assert normalise_utr("rzrpy3-482986") == "RZRPY3482986"


def test_normalise_drops_a_glued_bank_prefix() -> None:
    """Real narrations glue the instrument onto the reference. §6 names the prefixes."""
    assert normalise_utr("NEFTRZRPY3482986") == "RZRPY3482986"


def test_normalise_refuses_to_whittle_a_short_token() -> None:
    """Stripping a prefix off an already-short token would manufacture a reference that
    was never in the file, and Tier 0 posts at confidence 1.0. Better to leave it
    intact and let the token fail to match."""
    assert normalise_utr("CR123456") == "CR123456"


# --- token extraction -----------------------------------------------------------


def test_candidates_survive_a_delimiter_injected_into_the_reference() -> None:
    """NARRATION_NOISE inserts a hyphen *inside* the reference. Splitting the narration
    on every separator would yield 'RZRPY3' and '482986' and match nothing — Tier 0
    would look weak for a reason no test would explain."""
    assert "RZRPY3482986" in utr_candidates("RTGS-CR/HDFC/RZRPY3-482986/Orbit Digital/PNQ")


def test_candidates_exclude_purely_alphabetic_fields() -> None:
    """A customer name is not a reference. Admitting it would let Tier 0 match two
    unrelated rows that share a counterparty."""
    candidates = utr_candidates("NEFT-CR/HDFC/RZRPY1234567/BLUEPEAK LOGISTICS PVT LTD/BLR")

    assert "RZRPY1234567" in candidates
    assert not any(token.isalpha() for token in candidates)


def test_candidates_are_empty_when_the_reference_is_gone() -> None:
    """NO_UTR chaos. Tier 0 must find nothing here so the record reaches a later tier
    rather than being matched on something incidental."""
    assert utr_candidates("NEFT-CR/HDFC/ACME RETAIL PVT/BLR") == frozenset()


# --- the UTR rule ---------------------------------------------------------------


def test_exact_utr_match_posts_at_full_confidence() -> None:
    matches = match_tier0([bank()], [settlement()])

    assert len(matches) == 1
    assert matches[0].bank_txn_id == "BNK1"
    assert matches[0].settlement_ids == ("STL1",)
    assert matches[0].tier == 0
    assert matches[0].rule_id == RULE_UTR_EXACT
    assert matches[0].confidence == 1.0
    assert matches[0].evidence


def test_a_reference_match_with_a_mismatched_amount_falls_through() -> None:
    """The defect found on day 5, pinned so it cannot come back.

    A batched credit's narration carries only its *lead* settlement's reference.
    Matching on the reference alone pairs a credit covering N settlements with one of
    them, at confidence 1.0, and marks the credit resolved — orphaning the other
    members and under-explaining the credit. On the realistic fixture that was 25% of
    everything Tier 0 posted.

    A reference that matches while the money does not is evidence of a *batch*, not of
    a 1:1 match. It belongs to Tier 2.
    """
    matches = match_tier0(
        [bank("BNK1", credit=250_00)],
        [settlement("STL1", net=100_00)],
    )

    assert matches == []


def test_a_reference_match_with_an_exact_amount_still_posts() -> None:
    """The guard must not cost the matches Tier 0 legitimately makes."""
    matches = match_tier0([bank(credit=100_00)], [settlement(net=100_00)])

    assert _rules(matches) == [RULE_UTR_EXACT]


def test_tier0_does_not_absorb_paise_drift() -> None:
    """Tier 0 is the exact tier. A credit three paise off its settlement is Tier 1's
    problem, where a tolerance band exists precisely to absorb it — and where the match
    is recorded at 0.99 rather than asserting certainty it does not have."""
    matches = match_tier0([bank(credit=100_00 - 3)], [settlement(net=100_00)])

    assert matches == []


def test_a_utr_shared_by_two_settlements_matches_nothing() -> None:
    """§6's uniqueness guard, settlement side. Two settlements carrying one reference
    is a data problem; picking either is a coin flip on the books."""
    matches = match_tier0(
        [bank()], [settlement("STL1"), settlement("STL2")]
    )

    assert matches == []


def test_a_utr_appearing_in_two_narrations_matches_nothing() -> None:
    """Uniqueness guard, bank side — the direction that is easy to forget."""
    matches = match_tier0([bank("BNK1"), bank("BNK2")], [settlement()])

    assert matches == []


# --- the amount and date rule ---------------------------------------------------


def test_a_unique_amount_and_date_pair_matches() -> None:
    matches = match_tier0(
        [bank(narration="NEFT-CR/HDFC/NO REFERENCE HERE/BLR", credit=500_00)],
        [settlement(utr=None, net=500_00)],
    )

    assert _rules(matches) == [RULE_AMOUNT_DATE_UNIQUE]


def test_amount_and_date_falls_through_when_two_settlements_share_the_key() -> None:
    """This is what stops a DECOY_SUBSET pair being matched at Tier 0: a decoy is
    built from two settlements with identical nets on one date."""
    matches = match_tier0(
        [bank(narration="NEFT-CR/HDFC/NO REFERENCE/BLR", credit=500_00)],
        [settlement("STL1", utr=None, net=500_00), settlement("STL2", utr=None, net=500_00)],
    )

    assert matches == []


def test_amount_and_date_falls_through_when_two_credits_share_the_key() -> None:
    """And this is what stops a re-posted credit being matched: DUPLICATE_POST puts the
    same money on the same date under a second transaction id, so neither is unique."""
    matches = match_tier0(
        [
            bank("BNK1", narration="NEFT-CR/HDFC/NO REF/BLR", credit=500_00),
            bank("BNKDUP001", narration="NEFT-CR/HDFC/NO REF/BLR", credit=500_00),
        ],
        [settlement(utr=None, net=500_00)],
    )

    assert matches == []


def test_amount_matching_requires_the_dates_to_agree_exactly() -> None:
    """Tier 0 is the exact tier. Date slack belongs to Tier 1's window, where a fee
    model corroborates the amount."""
    matches = match_tier0(
        [bank(narration="NEFT-CR/HDFC/NO REF/BLR", credit=500_00, value_date=OTHER_DAY)],
        [settlement(utr=None, net=500_00, settled_on=DAY)],
    )

    assert matches == []


# --- interaction between the two rules ------------------------------------------


def test_the_utr_rule_wins_when_both_rules_could_fire() -> None:
    """A reference is direct evidence; an amount coincidence is not. Running the UTR
    rule first also shrinks the residual the amount rule may guess over."""
    matches = match_tier0(
        [bank("BNK1", credit=100_00)],
        [
            settlement("STL1", utr="RZRPY1234567", net=100_00),
            settlement("STL2", utr=None, net=777_00),
        ],
    )

    assert _rules(matches) == [RULE_UTR_EXACT]
    assert matches[0].settlement_ids == ("STL1",)


def test_a_settlement_is_never_posted_twice() -> None:
    """Two credits cannot both be explained by one settlement. Posting both would
    double-count the money and no later tier would notice."""
    matches = match_tier0(
        [
            bank("BNK1", narration="NEFT-CR/HDFC/RZRPY1234567/ACME/BLR", credit=100_00),
            bank("BNK2", narration="NEFT-CR/HDFC/NO REF/BLR", credit=100_00),
        ],
        [settlement("STL1", utr="RZRPY1234567", net=100_00)],
    )
    posted = [sid for match in matches for sid in match.settlement_ids]

    assert len(posted) == len(set(posted))


def test_nothing_matches_when_there_is_nothing_to_match() -> None:
    assert match_tier0([], []) == []
    assert match_tier0([bank()], []) == []
    assert match_tier0([], [settlement()]) == []
