"""Prompt version 1. The version string goes into every tier-3 provenance record.

TODO(day-9): write the adjudication prompt. It must state plainly that the model
may only choose from the supplied candidate IDs or return NO_MATCH, and that it
must not compute or adjust amounts.

Never edit a shipped prompt in place — add v2. A prompt change that is invisible
in the audit trail makes every past tier-3 match unexplainable.
"""

from __future__ import annotations
