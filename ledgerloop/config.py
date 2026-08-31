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

    subset_tolerance_bps: int = 0
    """Relative component of Tier 2's tolerance band. Deliberately zero.

    Tier 1 needs a relative band because it *recomputes* the expected net from the fee
    model and must absorb that imprecision. Tier 2 sums the settlements' **reported**
    nets, so there is no fee-model error to absorb — the only thing it must tolerate is
    PAISE_DRIFT, which is one to three paise, and the flat rupee covers that a hundred
    times over.

    A relative band here does not absorb error, it manufactures ambiguity: across a
    twenty-five settlement pool, a 0.5% window on a large credit admits combinatorially
    many near-misses that are not batches at all. Measured on `realistic` at 250 records,
    dropping this from 50 bps to 0 moved Tier 2 from 4 matches and 36 ambiguities to 41
    matches and 5. See ADR-019."""

    subset_max_members: int = 12
    """Largest batch Tier 2 will auto-post.

    Real batches are two to five payments. A credit covering thirteen is not something
    to resolve without a human looking, and the cap prunes the search hard."""

    subset_search_node_budget: int = 200_000
    """Nodes the subset search may explore before declining.

    The pool cap alone does not bound the work — 25 members still permits 2^25 nodes.
    This is the bound that is actually real, and §6 calls bounded compute an
    engineering signal. A search that exhausts it has not established uniqueness, so
    its result is discarded rather than posted."""

    max_llm_candidates: int = 8
    """How many candidates Tier 3 may see. Smaller is safer and cheaper."""


DEFAULT_MATCH_CONFIG = MatchConfig()
