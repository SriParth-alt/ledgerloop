"""Runs the same fixture under progressively enabled tiers.

TODO(day-8): T0 / T0+T1 / T0+T1+T2 / full / LLM-ONLY BASELINE.

The LLM-only baseline is not optional. It is the control arm that converts
'I built a cascade' into 'I measured that the cascade beats the obvious approach
on the metric that matters, using fewer model calls'. Without it the
architecture is an opinion.

Writes results/metrics.md. Never hand-edit that file.
"""

from __future__ import annotations
