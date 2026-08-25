"""Provenance record for every posted match.

Records tier, rule_id, evidence JSON, SHA-256 fingerprints of the source rows,
timestamp, operator (system|human), and for tier 3 the model name and prompt version.

Tier-3 matches stay permanently distinguishable from deterministic ones in the
UI. A reviewer must always be able to ask 'did a model touch this rupee?' and
get an answer.

That question has to be answerable with a ``WHERE`` clause, which is why the two
model columns are left genuinely NULL for deterministic matches rather than blank.
An empty string satisfies ``IS NOT NULL`` and would quietly corrupt the answer.

``source_fingerprints`` is what makes a decision reproducible after the CSVs are gone:
it cites the ``row_sha256`` of the bank row *and* of every settlement that fed the
match, so the exact inputs can be identified even if the files are lost or edited.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import Connection, text


@dataclass(frozen=True)
class MatchEvidence:
    """One field-level reason this bank row and this settlement correspond.

    Structured rather than prose because a human overturning a match needs to see
    *which* field was relied on, and the exception UI groups by it.
    """

    field: str
    bank_value: str
    settlement_value: str
    note: str = ""


@dataclass(frozen=True)
class ProposedMatch:
    """A match a tier wants to post. Not yet written."""

    bank_txn_id: str
    settlement_ids: tuple[str, ...]
    tier: int
    rule_id: str
    confidence: float
    evidence: tuple[MatchEvidence, ...] = ()


@dataclass(frozen=True)
class PostedMatch:
    """A match read back from the store, with its audit fields."""

    match_id: str
    bank_txn_id: str
    settlement_ids: tuple[str, ...]
    tier: int
    rule_id: str | None
    confidence: float
    evidence: tuple[MatchEvidence, ...]
    source_fingerprints: tuple[str, ...]
    model_name: str | None
    prompt_version: str | None
    operator: str


def _source_fingerprints(
    conn: Connection, run_id: str, *, bank_txn_id: str, settlement_ids: tuple[str, ...]
) -> list[str]:
    """Collect the stored row hashes of everything that fed this decision."""
    fingerprints: list[str] = []

    bank_hash = conn.execute(
        text("SELECT row_sha256 FROM bank_txns WHERE run_id = :run AND bank_txn_id = :id"),
        {"run": run_id, "id": bank_txn_id},
    ).scalar_one_or_none()
    if bank_hash is not None:
        fingerprints.append(bank_hash)

    for settlement_id in settlement_ids:
        settlement_hash = conn.execute(
            text(
                "SELECT row_sha256 FROM settlements "
                "WHERE run_id = :run AND settlement_id = :id"
            ),
            {"run": run_id, "id": settlement_id},
        ).scalar_one_or_none()
        if settlement_hash is not None:
            fingerprints.append(settlement_hash)

    return fingerprints


def record_match(
    conn: Connection,
    run_id: str,
    match: ProposedMatch,
    *,
    model_name: str | None = None,
    prompt_version: str | None = None,
    operator: str = "system",
) -> str:
    """Write a match record and return its id.

    ``model_name`` and ``prompt_version`` stay ``None`` for tiers 0-2 by construction:
    those tiers never call a model, so they have nothing to pass here.
    """
    match_id = str(uuid.uuid4())
    conn.execute(
        text(
            "INSERT INTO match_records (match_id, run_id, bank_txn_id, settlement_ids_json, "
            "tier, rule_id, confidence, evidence_json, source_fingerprints, model_name, "
            "prompt_version, operator, created_at) "
            "VALUES (:match_id, :run_id, :bank_txn_id, :settlement_ids_json, :tier, :rule_id, "
            ":confidence, :evidence_json, :source_fingerprints, :model_name, :prompt_version, "
            ":operator, :created_at)"
        ),
        {
            "match_id": match_id,
            "run_id": run_id,
            "bank_txn_id": match.bank_txn_id,
            "settlement_ids_json": json.dumps(list(match.settlement_ids)),
            "tier": match.tier,
            "rule_id": match.rule_id,
            "confidence": match.confidence,
            "evidence_json": json.dumps(
                [
                    {
                        "field": item.field,
                        "bank_value": item.bank_value,
                        "settlement_value": item.settlement_value,
                        "note": item.note,
                    }
                    for item in match.evidence
                ]
            ),
            "source_fingerprints": json.dumps(
                _source_fingerprints(
                    conn,
                    run_id,
                    bank_txn_id=match.bank_txn_id,
                    settlement_ids=match.settlement_ids,
                )
            ),
            "model_name": model_name,
            "prompt_version": prompt_version,
            "operator": operator,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        },
    )
    return match_id


def matches_for_run(conn: Connection, run_id: str) -> list[PostedMatch]:
    """Every match posted in a run, oldest first.

    Superseded records are included: the trail of what was decided and later overturned
    is the point of an append-only store, and hiding them here would put the audit
    trail one filter away from being lost.
    """
    rows = conn.execute(
        text(
            "SELECT match_id, bank_txn_id, settlement_ids_json, tier, rule_id, confidence, "
            "evidence_json, source_fingerprints, model_name, prompt_version, operator "
            "FROM match_records WHERE run_id = :run ORDER BY created_at, match_id"
        ),
        {"run": run_id},
    ).all()

    return [
        PostedMatch(
            match_id=row.match_id,
            bank_txn_id=row.bank_txn_id,
            settlement_ids=tuple(json.loads(row.settlement_ids_json)),
            tier=row.tier,
            rule_id=row.rule_id,
            confidence=row.confidence,
            evidence=tuple(
                MatchEvidence(
                    field=item["field"],
                    bank_value=item["bank_value"],
                    settlement_value=item["settlement_value"],
                    note=item.get("note", ""),
                )
                for item in json.loads(row.evidence_json)
            ),
            source_fingerprints=tuple(json.loads(row.source_fingerprints)),
            model_name=row.model_name,
            prompt_version=row.prompt_version,
            operator=row.operator,
        )
        for row in rows
    ]
