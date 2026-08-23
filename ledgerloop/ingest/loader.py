"""Fingerprinting and idempotent load.

TODO(day-3): SHA-256 each raw row (canonicalised: stripped, lowercased keys,
sorted) and use the digest as the natural key. Re-ingesting the same file must
be a no-op, and DUPLICATE_POST chaos must surface as DUPLICATE_SUSPECTED rather
than silently double-counting money.
"""

from __future__ import annotations
