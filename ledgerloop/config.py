"""Runtime configuration.

Tolerances and thresholds live here rather than scattered at call sites, because the
ablation table needs to vary them and the write-up needs to state them.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MatchConfig:
    """Tunable knobs for the cascade. Report these values alongside any metric."""

    amount_tolerance_paise: int = 100
    """Flat band: 1 rupee."""

    amount_tolerance_bps: int = 50
    """Relative band: 0.5%. The larger of the two applies."""

    settlement_slack_days: int = 3
    """Business days after ``settled_on`` in which a credit may still land.

    §6 specifies three. The window anchors on the settlement date the gateway
    reported rather than on a lag recomputed from ``captured_at`` — recomputing would
    assume the answer instead of using the reported fact.
    """

    fuzzy_utr_max_distance: int = 2
    """Levenshtein distance on the normalised UTR token."""

    name_similarity_threshold: int = 88
    """rapidfuzz token_set_ratio floor for a name to count as evidence."""

    tier1_auto_post_confidence: float = 0.90
    """Below this a Tier 1 composite score falls through instead of posting."""

    tier1_score_amount_and_date: float = 0.65
    """Contribution of the two gates. On its own this is deliberately below the
    auto-post threshold: amount and date agreeing is necessary but not sufficient,
    because many settlements share a window and a plausible amount."""

    tier1_score_fuzzy_reference: float = 0.25
    """Contribution of a reference within ``fuzzy_utr_max_distance``."""

    tier1_score_name: float = 0.09
    """Contribution of a counterparty-name match. The weakest signal and the most
    collision-prone — several settlements can share one customer."""

    subset_pool_cap: int = 25
    """Above this, decline with POOL_TOO_LARGE rather than search exponentially."""

    max_llm_candidates: int = 8
    """How many candidates Tier 3 may see. Smaller is safer and cheaper."""


DEFAULT_MATCH_CONFIG = MatchConfig()
