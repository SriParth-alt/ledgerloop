"""Chaos injectors — the realism layer.

TODO(day-2): implement each injector behind an independent flag so the ablation
table can attribute failures to specific real-world phenomena. See PROJECT_SPEC
section 5.5 for the full list.

BATCH, FEES, LAG, NARRATION_NOISE, NO_UTR, PARTIAL_REFUND, DUPLICATE_POST,
ORPHAN_CREDIT, OUT_OF_ORDER, PAISE_DRIFT, NAME_VARIANT, DECOY_SUBSET.

DECOY_SUBSET is the important one: it plants a second subset that sums to the
same credit. A naive matcher picks one and is wrong half the time. LedgerLoop
must raise AMBIGUOUS_SUBSET. Build this injector early — it is the demo.
"""

from __future__ import annotations
