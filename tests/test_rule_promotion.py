"""Rule promotion — the agentic loop.

This is the answer to "where is the agent?" (ADR-004). The cascade is deliberately not
agentic: it is arithmetic with one schema-constrained model call bolted onto the residual.
The agent is here — a human resolves an exception, the system works out *why the cascade
missed it*, proposes a generalised rule, and on approval persists it so the next run
resolves that whole class without being asked again.

Three properties carry the design:

**One resolution, one hypothesis.** A resolution is a single data point. The promoter
proposes exactly one generalised rule from it and says plainly what it inferred, in
natural language and in machine-readable form. Inferring three rules from one example is
how you get a store full of overfitted guesses.

**Approval is a gate, not a formality.** `propose_rule` persists nothing. A bad
generalisation from one resolution can create false matches at scale, and human approval
is the only thing standing in front of that.

**Promotion must be measurable.** §9.3 asks for auto-match rate before and after. A loop
that cannot be shown to change a number is a UI flourish, and the tests below insist the
delta is real rather than asserted.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from ledgerloop.generate.fee_model import PaymentMethod
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.rules.promote import (
    Resolution,
    Rule,
    RuleKind,
    RuleStore,
    load_rules,
    promote,
    propose_rule,
)

DAY = date(2026, 8, 10)
UTR = "RZRPY1234567"


def bank(*, narration: str, credit: int = 300) -> BankRow:
    return BankRow(
        bank_txn_id="BNK1",
        value_date=DAY,
        narration=narration,
        credit_paise=credit,
        debit_paise=0,
        balance_paise=credit,
    )


def settlement(*, utr: str | None = UTR, name: str = "ACME RETAIL PVT LTD") -> SettlementRow:
    return SettlementRow(
        settlement_id="STL1",
        payment_id="PAY1",
        order_id="ORD1",
        invoice_ref="INV1",
        customer_name=name,
        method=PaymentMethod.UPI,
        gross_amount_paise=300,
        fee_paise=0,
        gst_on_fee_paise=0,
        tds_paise=0,
        net_amount_paise=300,
        captured_at=DAY,
        settled_on=DAY,
        utr=utr,
        status="captured",
    )


# =================================================================================
# Proposing — what the system infers from one human decision
# =================================================================================


def test_an_unrecognised_instrument_prefix_becomes_a_prefix_rule() -> None:
    """The reference was there all along, glued to a prefix the normaliser did not know.

    This is the class §8 describes: not one broken row, one missing assumption. Every
    future credit from this bank carries the same prefix.
    """
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )

    assert proposal is not None
    assert proposal.kind is RuleKind.NARRATION_PREFIX
    assert proposal.value == "MMTCR"


def test_an_unrecognised_counterparty_spelling_becomes_an_alias() -> None:
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration="NEFT-CR/HDFC/ACME RTL P L/BLR"),
            settlement=settlement(utr=None, name="ACME RETAIL PVT LTD"),
            resolved_by="analyst",
        )
    )

    assert proposal is not None
    assert proposal.kind is RuleKind.COUNTERPARTY_ALIAS
    assert "ACME RTL P L" in proposal.value


def test_a_proposal_states_its_reasoning_in_words() -> None:
    """A rule a human is asked to approve must be readable without opening the code. The
    machine-readable form is what runs; the sentence is what gets approved.
    """
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )

    assert proposal is not None
    assert len(proposal.description) > 20
    assert "MMTCR" in proposal.description


def test_a_resolution_that_generalises_to_nothing_proposes_nothing() -> None:
    """Not every resolution contains a lesson. A one-off — a genuinely unique credit a
    human matched on judgement — must yield no rule rather than an overfitted one that
    fires on the next batch.
    """
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration="NEFT-CR/HDFC/UNRELATED PAYER/BLR"),
            settlement=settlement(utr="RZRPY9999999", name="NIMBUS TEXTILES LTD"),
            resolved_by="analyst",
        )
    )

    assert proposal is None


def test_one_resolution_yields_at_most_one_rule() -> None:
    """A resolution where both signals are missing could support two hypotheses. It
    proposes the stronger one, because inferring two rules from one example is how a
    store fills with guesses nobody approved individually.
    """
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RTL P L/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )

    assert proposal is not None
    assert isinstance(proposal, Rule)


# =================================================================================
# Approval — the gate in front of the store
# =================================================================================


def test_proposing_persists_nothing(tmp_path: Path) -> None:
    """ADR-004: a bad generalisation from one resolution can create false matches at
    scale, and human approval is the only gate on it."""
    store_path = tmp_path / "store.yaml"
    propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )

    assert load_rules(store_path).is_empty


def test_promotion_persists_an_approved_rule(tmp_path: Path) -> None:
    store_path = tmp_path / "store.yaml"
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )
    assert proposal is not None

    promote(proposal, store_path, approved_by="analyst")
    stored = load_rules(store_path)

    assert not stored.is_empty
    assert "MMTCR" in stored.narration_prefixes


def test_a_promoted_rule_records_who_approved_it(tmp_path: Path) -> None:
    """The store is committed to the repo so a reviewer can diff it after the demo and
    see exactly what the system learned. A rule with no author is a rule nobody owns."""
    store_path = tmp_path / "store.yaml"
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )
    assert proposal is not None
    promote(proposal, store_path, approved_by="parth")

    assert "parth" in store_path.read_text(encoding="utf-8")


def test_promoting_the_same_rule_twice_does_not_duplicate_it(tmp_path: Path) -> None:
    """The same class of exception recurs across runs. Each resolution would propose the
    same rule, and a store that grew a copy per resolution would slow every later run
    while learning nothing new."""
    store_path = tmp_path / "store.yaml"
    resolution = Resolution(
        bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
        settlement=settlement(),
        resolved_by="analyst",
    )
    proposal = propose_rule(resolution)
    assert proposal is not None

    promote(proposal, store_path, approved_by="analyst")
    promote(proposal, store_path, approved_by="analyst")

    assert len(load_rules(store_path).narration_prefixes) == 1


def test_an_empty_store_is_the_starting_state(tmp_path: Path) -> None:
    """`rules/store.yaml` ships with `rules: []` on purpose, so the diff after a demo
    shows precisely what was learned."""
    assert load_rules(tmp_path / "absent.yaml").is_empty


# =================================================================================
# Replay — the rule has to change what the cascade does
# =================================================================================


def test_a_promoted_prefix_makes_the_reference_recoverable(tmp_path: Path) -> None:
    """The whole point. Before promotion the normaliser cannot see the reference; after
    it, it can — and that is the measurable lift §9.3 asks for, not a UI flourish.
    """
    from ledgerloop.cascade.tier0_exact import normalise_utr

    narration_token = f"MMTCR{UTR}"

    assert normalise_utr(narration_token) != UTR

    store_path = tmp_path / "store.yaml"
    proposal = propose_rule(
        Resolution(
            bank_txn=bank(narration=f"{narration_token}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )
    assert proposal is not None
    promote(proposal, store_path, approved_by="analyst")

    assert normalise_utr(narration_token, rules=load_rules(store_path)) == UTR


def test_an_unpromoted_proposal_changes_nothing(tmp_path: Path) -> None:
    """Approval is what makes a rule live. A proposal sitting unapproved must not alter
    a single match."""
    from ledgerloop.cascade.tier0_exact import normalise_utr

    propose_rule(
        Resolution(
            bank_txn=bank(narration=f"MMTCR{UTR}/ACME RETAIL PVT LTD/BLR"),
            settlement=settlement(),
            resolved_by="analyst",
        )
    )

    assert normalise_utr(f"MMTCR{UTR}", rules=load_rules(tmp_path / "store.yaml")) != UTR


def test_rules_never_shorten_a_token_below_the_safety_floor(tmp_path: Path) -> None:
    """A promoted prefix is still subject to Tier 0's floor. Otherwise one approved rule
    could whittle short tokens into references that were never in the file — and Tier 0
    posts at confidence 1.0.
    """
    from ledgerloop.cascade.tier0_exact import normalise_utr

    store_path = tmp_path / "store.yaml"
    promote(
        Rule(
            kind=RuleKind.NARRATION_PREFIX,
            value="ABCD",
            description="test rule",
            learned_from="BNK1",
        ),
        store_path,
        approved_by="analyst",
    )

    assert normalise_utr("ABCD1234", rules=load_rules(store_path)) == "ABCD1234"


def test_the_store_round_trips(tmp_path: Path) -> None:
    store_path = tmp_path / "store.yaml"
    promote(
        Rule(
            kind=RuleKind.COUNTERPARTY_ALIAS,
            value="ACME RTL P L=ACME RETAIL PVT LTD",
            description="analyst confirmed these name the same customer",
            learned_from="BNK1",
        ),
        store_path,
        approved_by="analyst",
    )
    reloaded: RuleStore = load_rules(store_path)

    assert reloaded.alias_for("ACME RTL P L") == "ACME RETAIL PVT LTD"


def test_an_unknown_name_has_no_alias(tmp_path: Path) -> None:
    assert load_rules(tmp_path / "store.yaml").alias_for("SOMEONE ELSE") is None


@pytest.mark.parametrize("kind", list(RuleKind))
def test_every_rule_kind_survives_a_write_and_read(tmp_path: Path, kind: RuleKind) -> None:
    """A kind that cannot round-trip would be silently dropped on the next run, and the
    lift measured on day 11 would decay for no visible reason."""
    store_path = tmp_path / f"{kind.value}.yaml"
    promote(
        Rule(kind=kind, value="A=B" if kind is RuleKind.COUNTERPARTY_ALIAS else "WXYZ",
             description="round trip", learned_from="BNK1"),
        store_path,
        approved_by="analyst",
    )

    assert not load_rules(store_path).is_empty
