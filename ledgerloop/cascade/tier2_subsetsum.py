"""Tier 2 — subset-sum for batched payouts. The technically interesting tier.

TODO(day-6): for each unmatched bank credit, build a candidate pool (same
merchant, inside the date window), prune by nearest date to <= subset_pool_cap,
then find every subset whose net sum is within tolerance of the credit.

Bounded DP over paise first. Meet-in-the-middle O(2^(N/2)) is an optimisation,
not a prerequisite — ship the DP on day 6 and optimise on day 7 only if time
allows.

TODO(day-7): the ambiguity guard, which matters more than the search itself.
  exactly one solution -> post, confidence 0.95, evidence lists every member
  two or more         -> AMBIGUOUS_SUBSET with ALL explanations attached
  zero                -> fall through to Tier 3
  pool over cap       -> POOL_TOO_LARGE, decline rather than search

Do not rank ambiguous solutions and pick a winner. Both are arithmetically
perfect; there is no principled tiebreak; picking is a coin flip on the books.

WRITE THIS TIER YOURSELF BEFORE LETTING AN AGENT NEAR IT. It is the piece a
panel is most likely to ask you to derive on a whiteboard.
"""

from __future__ import annotations
