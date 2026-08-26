"""Tier 1 — tolerant deterministic matching. Still no LLM.

Amount within the fee-model tolerance, date inside the settlement window, fuzzy UTR
within Levenshtein 2, name via rapidfuzz token_set_ratio.

Composite score auto-posts at >= ``tier1_auto_post_confidence``, otherwise falls
through. ``expected_net`` is recomputed with the SAME FeeModel the generator used —
if these drift, the matcher works on synthetic data for the wrong reason.

**Amount and date are gates, not weights.** §6 describes a weighted composite, which
taken literally would let a match clear the threshold on reference and name alone while
the money does not reconcile. ADR-002 says arithmetic decides, so scoring only ever
ranks candidates that already reconcile. A wrong amount cannot be outvoted.

**Where the weights come from.** They are not tuned. Each contribution is set so the
chaos injector §5.5 assigns to a tier lands in that tier:

* amount + date + fuzzy reference = 0.90 → posts, which is `NARRATION_NOISE`
* amount + date + name            = 0.74 → falls through, which is `NO_UTR` reaching
  Tier 3 exactly as §5.5 intends
* amount + date alone             = 0.65 → falls through

The ceiling is 0.99. Confidence 1.0 means "unimpeachable" and belongs to Tier 0 alone;
a tolerant match that claimed certainty would make the audit trail lie about how it was
made.

**A tie is declined.** When two settlements score identically for one credit, the
ordering between them is something we invented, and picking one is the coin flip
ADR-003 forbids. It falls through silently rather than raising `AMBIGUOUS_SUBSET` —
that code means subset ambiguity, and Tier 2 may yet explain the credit as a batch.

**Partial refunds fall through by design.** ``expected_net`` is recomputed from gross,
and the fee model cannot know the size of a refund that netted against the same cycle.
Those credits reach Tier 2, whose subset arithmetic works from reported nets. §5.5
suggests Tier 1 might catch some; it does not, and day 8 will show what that costs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from rapidfuzz.distance import Levenshtein
from rapidfuzz.fuzz import token_set_ratio

from ledgerloop.audit.provenance import MatchEvidence, ProposedMatch
from ledgerloop.cascade.tier0_exact import FIELD_SPLIT, normalise_utr, utr_candidates
from ledgerloop.config import DEFAULT_MATCH_CONFIG, MatchConfig
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, FeeModel
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.money import within_tolerance

RULE_TOLERANT = "T1-TOLERANT"

TIER = 1

#: Scores are compared against a threshold, so they are rounded before comparison.
#: 0.65 + 0.25 is not exactly 0.90 in binary floating point, and a match that fell
#: through by one part in 10^16 would be maddening to diagnose.
_SCORE_PRECISION = 4


@dataclass(frozen=True)
class Tier1Score:
    """One settlement's case for explaining one credit."""

    settlement_id: str
    score: float
    evidence: tuple[MatchEvidence, ...]


def expected_net_paise(settlement: SettlementRow, *, fee_model: FeeModel) -> int:
    """What the fee model says should have reached the bank for this settlement.

    Recomputed from ``gross`` rather than read from the settlement's own
    ``net_amount_paise``. Reading the reported figure would be trusting the document
    we are supposed to be checking; recomputing is what makes this a reconciliation.
    """
    return fee_model.net_paise(settlement.gross_amount_paise, settlement.method)


def amount_agrees(
    credit_paise: int,
    settlement: SettlementRow,
    *,
    fee_model: FeeModel,
    config: MatchConfig,
) -> bool:
    """True when the credit lands inside the fee-model tolerance band.

    The band is the larger of a flat rupee and 0.5%: a flat band alone is too tight on
    large settlements, a relative one too tight on small ones.
    """
    return within_tolerance(
        credit_paise,
        expected_net_paise(settlement, fee_model=fee_model),
        abs_paise=config.amount_tolerance_paise,
        rel_bps=config.amount_tolerance_bps,
    )


def date_in_window(
    bank_txn: BankRow,
    settlement: SettlementRow,
    *,
    fee_model: FeeModel,
    config: MatchConfig,
) -> bool:
    """True when the credit landed on or after settlement, within the slack window.

    Money cannot arrive before the gateway released it, so an earlier credit is
    evidence of a different payment rather than of an early one. The upper bound goes
    through ``business_days_after`` so weekends and holidays are handled in one place.
    """
    if bank_txn.value_date < settlement.settled_on:
        return False
    latest = fee_model.business_days_after(
        settlement.settled_on, config.settlement_slack_days
    )
    return bank_txn.value_date <= latest


def reference_distance(narration: str, utr: str | None) -> int | None:
    """Edit distance from the settlement's reference to the closest token in the
    narration, or ``None`` when there is nothing to compare.

    Absent evidence must read as absent rather than as a distant match — otherwise
    ``NO_UTR`` rows would score as merely weak references instead of missing ones.
    """
    if not utr:
        return None
    target = normalise_utr(utr)
    candidates = utr_candidates(narration)
    if not target or not candidates:
        return None
    return min(Levenshtein.distance(target, candidate) for candidate in candidates)


def name_similarity(customer_name: str, narration: str) -> int:
    """rapidfuzz token-set ratio between the counterparty and the best narration field.

    Compared field by field rather than against the whole string. ``token_set_ratio``
    splits on whitespace, and narration fields are separated by ``/`` — so a
    whole-string comparison sees ``NEFT-CR/HDFC/RZRPY1234567/ACME`` as a single token
    and scores a perfect name match at around fifty. Comparing per field also stops
    the bank code and branch from diluting a genuine match.
    """
    target = customer_name.upper()
    ratios = [
        round(token_set_ratio(target, field.strip().upper()))
        for field in FIELD_SPLIT.split(narration)
        if field.strip()
    ]
    return max(ratios, default=0)


def score_candidate(
    bank_txn: BankRow,
    settlement: SettlementRow,
    *,
    fee_model: FeeModel,
    config: MatchConfig,
) -> Tier1Score | None:
    """Score one pairing, or return ``None`` if it fails a gate.

    Returning ``None`` rather than a low score is deliberate: a failed gate is not weak
    evidence, it is disqualifying, and collapsing the two would let a wrong amount be
    outvoted by a good name.
    """
    if not amount_agrees(
        bank_txn.credit_paise, settlement, fee_model=fee_model, config=config
    ):
        return None
    if not date_in_window(bank_txn, settlement, fee_model=fee_model, config=config):
        return None

    expected = expected_net_paise(settlement, fee_model=fee_model)
    score = config.tier1_score_amount_and_date
    evidence = [
        MatchEvidence(
            field="expected_net_paise",
            bank_value=str(bank_txn.credit_paise),
            settlement_value=str(expected),
            note="gross less MDR, GST and TDS, recomputed; within tolerance",
        ),
        MatchEvidence(
            field="value_date",
            bank_value=bank_txn.value_date.isoformat(),
            settlement_value=settlement.settled_on.isoformat(),
            note=f"inside {config.settlement_slack_days} business days of settlement",
        ),
    ]

    distance = reference_distance(bank_txn.narration, settlement.utr)
    if distance is not None and distance <= config.fuzzy_utr_max_distance:
        score += config.tier1_score_fuzzy_reference
        evidence.append(
            MatchEvidence(
                field="narration_token",
                bank_value=bank_txn.narration,
                settlement_value=settlement.utr or "",
                note=f"reference recovered at edit distance {distance}",
            )
        )

    similarity = name_similarity(settlement.customer_name, bank_txn.narration)
    if similarity >= config.name_similarity_threshold:
        score += config.tier1_score_name
        evidence.append(
            MatchEvidence(
                field="customer_name",
                bank_value=bank_txn.narration,
                settlement_value=settlement.customer_name,
                note=f"token-set ratio {similarity}",
            )
        )

    return Tier1Score(
        settlement_id=settlement.settlement_id,
        score=round(score, _SCORE_PRECISION),
        evidence=tuple(evidence),
    )


def match_tier1(
    bank_txns: Sequence[BankRow],
    settlements: Sequence[SettlementRow],
    *,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
) -> list[ProposedMatch]:
    """Score every reconcilable pairing, then assign the unambiguous ones."""
    by_bank: dict[str, list[Tier1Score]] = {}
    for bank_txn in bank_txns:
        candidates = [
            scored
            for settlement in settlements
            if (
                scored := score_candidate(
                    bank_txn, settlement, fee_model=fee_model, config=config
                )
            )
            is not None
            and scored.score >= config.tier1_auto_post_confidence
        ]
        candidates.sort(key=lambda item: (-item.score, item.settlement_id))
        by_bank[bank_txn.bank_txn_id] = candidates

    # Strongest cases first, so a credit with unambiguous evidence claims its
    # settlement before a weaker one can contest it.
    order = sorted(
        bank_txns,
        key=lambda row: (
            -(by_bank[row.bank_txn_id][0].score if by_bank[row.bank_txn_id] else 0.0),
            row.bank_txn_id,
        ),
    )

    proposals: list[ProposedMatch] = []
    claimed: set[str] = set()
    for bank_txn in order:
        available = [
            scored
            for scored in by_bank[bank_txn.bank_txn_id]
            if scored.settlement_id not in claimed
        ]
        if not available:
            continue

        best = available[0]
        if len(available) > 1 and available[1].score == best.score:
            # A genuine tie. Any ordering we impose here is invented.
            continue

        proposals.append(
            ProposedMatch(
                bank_txn_id=bank_txn.bank_txn_id,
                settlement_ids=(best.settlement_id,),
                tier=TIER,
                rule_id=RULE_TOLERANT,
                confidence=best.score,
                evidence=best.evidence,
            )
        )
        claimed.add(best.settlement_id)

    return proposals
