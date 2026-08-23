"""Promote a human resolution into a reusable rule. This is the agentic loop.

TODO(day-11): when a human resolves an exception, inspect the resolution and
propose a GENERALISED rule in both natural language and machine-readable form.
On approval, persist to rules/store.yaml and replay on the next run.

Measure the lift: auto-match rate before and after promoting five rules. That
delta is the evidence the loop does something, rather than being a UI flourish.
"""

from __future__ import annotations
