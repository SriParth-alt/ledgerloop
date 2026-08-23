"""Tier 3 — constrained LLM adjudication of the residual only.

TODO(day-9): assemble at most `max_llm_candidates` pre-scored candidates, call
the adapter at temperature 0, then run every response through
`cascade.gates.run_all_gates`. Nothing reaches the ledger except through the
gates.

The model receives: one bank row (raw narration included), the candidates, the
fee model, and the date window. It returns an Adjudication or it returns
nothing usable. It does not compute amounts. It does not resolve ambiguity.
It does not see records that Tiers 0-2 already settled.
"""

from __future__ import annotations
