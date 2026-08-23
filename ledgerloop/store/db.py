"""SQLite connection management.

TODO(day-3): WAL mode, foreign keys ON, and a `run_id` on every write so a
reconciliation run is a first-class, comparable object.

Tables are APPEND-ONLY. Corrections are new rows superseding old ones, never
UPDATEs. Provenance depends on this — an UPDATE destroys the audit trail you
are going to demo.
"""

from __future__ import annotations
