"""The Tier 3 output contract.

These tests exist to prove the schema gate cannot be talked around. They should keep
passing unchanged as Tier 3 gets implemented on day 9.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ledgerloop.llm.contract import Adjudication, Evidence


def _evidence() -> Evidence:
    return Evidence(
        field_name="narration_token",
        bank_value="RZRPY0034821",
        settlement_value="rzrpy0034821",
        reasoning="same UTR after normalisation",
    )


class TestValidProposals:
    def test_accepts_a_well_formed_match(self) -> None:
        adj = Adjudication(
            decision="MATCH",
            matched_settlement_ids=["setl_001"],
            evidence=[_evidence()],
            confidence=0.92,
        )
        assert adj.decision == "MATCH"

    def test_accepts_a_well_formed_no_match(self) -> None:
        adj = Adjudication(
            decision="NO_MATCH",
            confidence=0.1,
            unresolved_reason="no candidate shares a reference or plausible amount",
        )
        assert adj.matched_settlement_ids == []


class TestRejectedProposals:
    def test_match_without_ids_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(decision="MATCH", evidence=[_evidence()], confidence=0.9)

    def test_match_without_evidence_is_invalid(self) -> None:
        """A match with no evidence is unauditable, which makes it useless later."""
        with pytest.raises(ValidationError):
            Adjudication(
                decision="MATCH", matched_settlement_ids=["setl_001"], confidence=0.9
            )

    def test_no_match_naming_ids_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(
                decision="NO_MATCH",
                matched_settlement_ids=["setl_001"],
                confidence=0.2,
                unresolved_reason="unsure",
            )

    def test_no_match_without_reason_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(decision="NO_MATCH", confidence=0.2)

    def test_duplicate_ids_are_invalid(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(
                decision="MATCH",
                matched_settlement_ids=["setl_001", "setl_001"],
                evidence=[_evidence()],
                confidence=0.9,
            )

    @pytest.mark.parametrize("bad_confidence", [-0.1, 1.1, 2.0])
    def test_confidence_must_be_a_probability(self, bad_confidence: float) -> None:
        with pytest.raises(ValidationError):
            Adjudication(
                decision="MATCH",
                matched_settlement_ids=["setl_001"],
                evidence=[_evidence()],
                confidence=bad_confidence,
            )

    def test_unknown_decision_is_invalid(self) -> None:
        with pytest.raises(ValidationError):
            Adjudication(decision="MAYBE", confidence=0.5)  # type: ignore[arg-type]
