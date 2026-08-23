"""Quarantine for rows that fail schema validation.

TODO(day-3): store raw text + validation error + source file + line number.
Quarantined rows appear in the run report. A batch that quarantines 40 rows
and reports a 95% match rate on the rest is lying by omission.
"""

from __future__ import annotations
