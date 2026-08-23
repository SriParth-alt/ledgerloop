"""Tier 0 — exact deterministic matching. No LLM.

TODO(day-4): normalise UTRs (uppercase, strip non-alphanumerics, drop NEFT/IMPS/
RTGS/CR/UPI prefixes), extract candidate tokens from narration by length and
charset, then match on exact normalised UTR or on an exactly-unique
(net_amount_paise, value_date) pair.

UNIQUENESS GUARD: if a key maps to more than one candidate on either side, do
not match here. Fall through. Tier 0 exists to be unimpeachable.
"""

from __future__ import annotations
