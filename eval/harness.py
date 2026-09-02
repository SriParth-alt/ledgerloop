"""Scores a run against generator ground truth.

BUILT BEFORE TIER 3, as the TODO demanded. Without measurement you cannot tell whether
the LLM helped, and the whole argument of the project is a measurement.

This package is the ONLY one permitted to read truth_links.csv. There is a test
enforcing it (tests/test_no_truth_leak.py). Do not weaken that test — it matters more
now than it did yesterday, because until today nothing was on this side of the boundary
and the guard had nothing to leak. It is now the only thing stopping the capability
drifting sideways into the matcher.

Scoring reads what was **persisted**. A match that never reached ``match_records`` did
not happen, and the provenance trail is part of what is being measured.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Connection, text

from eval.metrics import RunMetrics, compute_metrics


@dataclass(frozen=True)
class Truth:
    """What actually happened, as the generator knows it.

    ``explanations`` maps a bank credit to the set of settlements that explain it. A
    batch of three is three rows in the file and one entry here — scoring against the
    rows rather than the sets is what would make a partially matched batch look
    partially correct.

    An orphan credit is present with an **empty** set. Absent would mean "we do not
    know"; empty means "we know there is nothing", and only the second is true.
    """

    explanations: dict[str, frozenset[str]]
    chaos_tags: dict[str, tuple[str, ...]]


def load_truth(path: Path) -> Truth:
    """Read ``truth_links.csv`` and group it by credit."""
    grouped: dict[str, set[str]] = {}
    tags: dict[str, set[str]] = {}

    with Path(path).open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            credit = row["bank_txn_id"]
            members = grouped.setdefault(credit, set())
            settlement = (row.get("settlement_id") or "").strip()
            if settlement:
                members.add(settlement)

            recorded = tags.setdefault(credit, set())
            for tag in (row.get("chaos_tags") or "").split("|"):
                if tag.strip():
                    recorded.add(tag.strip())

    return Truth(
        explanations={credit: frozenset(members) for credit, members in grouped.items()},
        chaos_tags={credit: tuple(sorted(found)) for credit, found in tags.items()},
    )


def score_run(
    conn: Connection,
    run_id: str,
    truth: Truth,
    *,
    seconds: float,
    llm_invocations: int = 0,
    cache_hits: int = 0,
    hallucinations: int = 0,
    cost_paise: int | None = None,
) -> RunMetrics:
    """Score what a run persisted against what actually happened."""
    posted: dict[str, frozenset[str]] = {}
    for row in conn.execute(
        text(
            "SELECT bank_txn_id, settlement_ids_json FROM match_records "
            "WHERE run_id = :run AND superseded_by IS NULL"
        ),
        {"run": run_id},
    ):
        posted[row.bank_txn_id] = frozenset(json.loads(row.settlement_ids_json))

    exceptions: dict[str, str] = {}
    for row in conn.execute(
        text(
            "SELECT bank_txn_id, reason_code FROM exceptions "
            "WHERE run_id = :run AND bank_txn_id IS NOT NULL AND resolved_at IS NULL"
        ),
        {"run": run_id},
    ):
        exceptions[row.bank_txn_id] = row.reason_code

    credit_values = {
        row.bank_txn_id: row.credit_paise
        for row in conn.execute(
            text("SELECT bank_txn_id, credit_paise FROM bank_txns WHERE run_id = :run"),
            {"run": run_id},
        )
    }

    return compute_metrics(
        posted=posted,
        truth=truth.explanations,
        credit_values=credit_values,
        exceptions=exceptions,
        seconds=seconds,
        llm_invocations=llm_invocations,
        cache_hits=cache_hits,
        hallucinations=hallucinations,
        cost_paise=cost_paise,
    )
