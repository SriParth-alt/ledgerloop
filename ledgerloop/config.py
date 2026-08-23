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

    settlement_slack_days: int = 1
    """Extra business days beyond T+N that a credit may still land in."""

    fuzzy_utr_max_distance: int = 2
    """Levenshtein distance on the normalised UTR token."""

    name_similarity_threshold: int = 88
    """rapidfuzz token_set_ratio floor for a name to count as evidence."""

    tier1_auto_post_confidence: float = 0.90
    """Below this a Tier 1 composite score falls through instead of posting."""

    subset_pool_cap: int = 25
    """Above this, decline with POOL_TOO_LARGE rather than search exponentially."""

    max_llm_candidates: int = 8
    """How many candidates Tier 3 may see. Smaller is safer and cheaper."""


DEFAULT_MATCH_CONFIG = MatchConfig()
