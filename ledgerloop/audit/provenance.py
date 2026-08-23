"""Provenance record for every posted match.

TODO(day-4): tier, rule_id, evidence JSON, SHA-256 fingerprints of the source
rows, timestamp, operator (system|human), and for tier 3 the model name and
prompt version.

Tier-3 matches stay permanently distinguishable from deterministic ones in the
UI. A reviewer must always be able to ask 'did a model touch this rupee?' and
get an answer.
"""

from __future__ import annotations
