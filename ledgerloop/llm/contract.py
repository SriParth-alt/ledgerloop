"""The Tier 3 output contract.

The model is a *proposer of evidence*, never a decider. This schema is the narrowest
surface through which it is allowed to speak: one decision, IDs drawn only from the
supplied candidate set, and an explicit evidence trail for each claimed
correspondence.

Anything that does not validate against this becomes an ``LLM_INVALID_OUTPUT``
exception. There is no retry-until-parseable loop.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class Evidence(BaseModel):
    """One claimed correspondence between a bank row and a settlement row.

    Requiring per-field evidence is not decoration. It is what makes a tier-3 match
    auditable months later, and it makes the model's reasoning inspectable in the
    exception UI when a human overturns it.
    """

    field_name: str = Field(
        description="Which field corresponds, e.g. 'narration_token', 'amount', 'customer_name'."
    )
    bank_value: str
    settlement_value: str
    reasoning: str = Field(max_length=200)


class Adjudication(BaseModel):
    """The complete permitted response from the Tier 3 model."""

    decision: Literal["MATCH", "NO_MATCH"]
    matched_settlement_ids: list[str] = Field(default_factory=list)
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    unresolved_reason: str | None = None

    @model_validator(mode="after")
    def check_internal_consistency(self) -> Adjudication:
        """Structural coherence only — this is not the membership or arithmetic gate.

        A MATCH with no IDs, or a NO_MATCH that nonetheless names IDs, means the
        model did not understand the task. Treat it as invalid rather than trying
        to interpret intent.
        """
        if self.decision == "MATCH":
            if not self.matched_settlement_ids:
                raise ValueError("decision=MATCH requires at least one settlement id")
            if not self.evidence:
                raise ValueError("decision=MATCH requires at least one evidence item")
        else:
            if self.matched_settlement_ids:
                raise ValueError("decision=NO_MATCH must not name settlement ids")
            if not self.unresolved_reason:
                raise ValueError("decision=NO_MATCH requires an unresolved_reason")

        if len(set(self.matched_settlement_ids)) != len(self.matched_settlement_ids):
            raise ValueError("duplicate settlement ids in proposal")

        return self


#: Below this, a proposal becomes a LOW_CONFIDENCE exception rather than a match.
#: Tune this deliberately and report the effect in the ablation table — it is the
#: dial that trades auto-match rate against false-match rate.
CONFIDENCE_THRESHOLD = 0.85
