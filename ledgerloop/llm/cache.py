"""Response cache keyed on SHA-256 of the prompt payload.

TODO(day-9): reconciliation must be reproducible for audit. A re-run of the same
batch performs zero new API calls and produces a byte-identical match set.

Commit the cached fixture responses so CI runs Tier 3 without an API key and
without cost.
"""

from __future__ import annotations
