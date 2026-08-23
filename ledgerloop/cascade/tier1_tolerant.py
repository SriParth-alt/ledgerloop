"""Tier 1 — tolerant deterministic matching. Still no LLM.

TODO(day-5): amount within the fee-model tolerance, date inside
FeeModel.settlement_window, fuzzy UTR within Levenshtein 2, name via rapidfuzz
token_set_ratio.

Composite score auto-posts at >= tier1_auto_post_confidence, otherwise falls
through. Recompute expected net with the SAME FeeModel the generator used —
if these drift, the matcher works on synthetic data for the wrong reason.
"""

from __future__ import annotations
