"""FastAPI surface for the exception queue UI.

TODO(day-12): endpoints for run summary, exception queue (sorted by value at
risk), match provenance lookup, and resolve-plus-promote.

BUFFER POLICY: if day 12 slips, cut this and generate a static HTML report from
the CLI instead. A CLI with real measured numbers beats a pretty UI with none.
"""

from __future__ import annotations
