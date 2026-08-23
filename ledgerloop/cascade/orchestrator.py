"""Runs the tiers in order and records the outcome of every record.

TODO(day-4, extended each day): execute T0 -> T1 -> T2 -> T3 -> T4, emitting a
per-tier count as it goes (this streaming output is the demo).

Support `--tiers 0,1,2` so the ablation harness can run partial configurations
against the same fixture. Support `--no-llm` producing a degraded=true run that
still completes: if the model is unavailable the batch finishes without Tier 3,
auto-match rate falls, correctness does not.
"""

from __future__ import annotations
