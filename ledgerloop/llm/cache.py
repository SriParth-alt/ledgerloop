"""Response cache keyed on SHA-256 of the prompt payload.

Reconciliation must be reproducible for audit. A re-run of the same batch performs zero
new API calls and produces a byte-identical match set.

That guarantee has a prerequisite most caches do not: **the prompt must be byte-identical
between runs**. Tier 3 sorts its candidate list deterministically for exactly this
reason. If candidate order drifted, every re-run would miss the cache and pay again — and
worse, could receive a different answer to a question that had not really changed.

Commit the cached fixture responses so CI runs Tier 3 without an API key and without
cost. A cache directory of ``None`` disables persistence entirely, which is what unit
tests use when they are not testing the cache itself.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


def cache_key(prompt: str, *, model: str = "") -> str:
    """SHA-256 over the full payload, model included.

    The model is part of the key because the same prompt answered by a different model is
    a different answer. Serving one from the other's cache would make the provenance
    record's ``model_name`` untrue.
    """
    payload = json.dumps({"model": model, "prompt": prompt}, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ResponseCache:
    """A directory of responses, one file per prompt fingerprint.

    Deliberately a directory of small files rather than one blob: a reviewer can open a
    single cached response and read exactly what the model said about one credit, and
    committing the fixture cache produces a readable diff rather than an opaque one.
    """

    def __init__(self, directory: Path | None) -> None:
        self.directory = Path(directory) if directory is not None else None
        if self.directory is not None:
            self.directory.mkdir(parents=True, exist_ok=True)

    def get(self, key: str) -> str | None:
        if self.directory is None:
            return None
        path = self.directory / f"{key}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))["response"]

    def put(self, key: str, response: str) -> None:
        if self.directory is None:
            return
        path = self.directory / f"{key}.json"
        path.write_text(
            json.dumps({"key": key, "response": response}, indent=2),
            encoding="utf-8",
            newline="\n",
        )
