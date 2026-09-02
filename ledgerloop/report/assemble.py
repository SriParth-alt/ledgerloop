"""Gather everything the report shows out of one run.

Reads the database and the committed cache. Does **not** read ground truth — accuracy
arrives as an already-scored ``metrics`` object from `eval/`, which is the only package
permitted to compute it (rule 6). That split is what lets the report live beside the
matcher without the matcher gaining a capability it must not have.
"""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path

from sqlalchemy import Connection, text

from ledgerloop.exceptions.clustering import cluster, open_exceptions
from ledgerloop.report.model import (
    AblationRow,
    Adjudication,
    ClusterRow,
    EvidenceItem,
    GateStage,
    ProvenanceRow,
    ReportData,
    RunHeader,
    TierStep,
)
from ledgerloop.report.replay import replay_tier3

#: §7.3 requires demonstrating that a fabricated identifier discards the *whole* response.
#: Across every measured arm and both fixtures the model never fabricated one — zero
#: hallucinated ids — so this case is constructed rather than replayed, and it is marked
#: ``scripted`` so the page never implies a real model produced it.
_FABRICATED_ID = "STL99999"


def _tiers(conn: Connection, run_id: str) -> tuple[list[TierStep], int]:
    rows = conn.execute(
        text(
            "SELECT tier, COUNT(*) AS credits, "
            "SUM(json_array_length(settlement_ids_json)) AS settlements "
            "FROM match_records WHERE run_id = :run GROUP BY tier ORDER BY tier"
        ),
        {"run": run_id},
    ).all()
    steps = [TierStep(tier=r[0], credits=r[1], settlements=r[2] or 0) for r in rows]

    total = conn.execute(
        text("SELECT COUNT(*) FROM bank_txns WHERE run_id = :run"), {"run": run_id}
    ).scalar_one()
    return steps, total - sum(s.credits for s in steps)


def _provenance(conn: Connection, run_id: str, limit: int) -> list[ProvenanceRow]:
    """A sample across tiers rather than the first N.

    Taking them in insertion order would return only Tier 0 rows, and the point of the
    panel is that every tier carries a trail — including the one that used a model.
    """
    rows = conn.execute(
        text(
            "SELECT bank_txn_id, settlement_ids_json, tier, rule_id, confidence, "
            "evidence_json, source_fingerprints, model_name, prompt_version "
            "FROM match_records WHERE run_id = :run "
            "ORDER BY tier, bank_txn_id"
        ),
        {"run": run_id},
    ).all()

    per_tier: dict[int, list[ProvenanceRow]] = {}
    for r in rows:
        per_tier.setdefault(r[2], []).append(
            ProvenanceRow(
                bank_txn_id=r[0],
                settlement_ids=tuple(json.loads(r[1])),
                tier=r[2],
                rule_id=r[3] or "",
                confidence=r[4],
                evidence=[
                    EvidenceItem(
                        field_name=item.get("field", ""),
                        bank_value=str(item.get("bank_value", "")),
                        settlement_value=str(item.get("settlement_value", "")),
                        note=item.get("note", ""),
                    )
                    for item in json.loads(r[5])
                ],
                fingerprints=tuple(json.loads(r[6])),
                model_name=r[7],
                prompt_version=r[8],
            )
        )

    take = max(1, limit // max(len(per_tier), 1))
    out: list[ProvenanceRow] = []
    for tier in sorted(per_tier):
        out.extend(per_tier[tier][:take])
    return out[:limit]


def _clusters(conn: Connection, run_id: str) -> list[ClusterRow]:
    items = open_exceptions(conn, run_id)
    return [
        ClusterRow(
            code=c.code.value,
            count=c.count,
            value_at_risk_paise=c.value_at_risk_paise,
            diagnosis=c.diagnosis,
        )
        for c in cluster(items)
    ]


def _scripted_hallucination(base: Adjudication) -> Adjudication:
    """Reuse a real prompt and pool, and script only the response.

    Everything the model was *given* stays real; only what it returned is constructed. A
    wholly invented case would demonstrate the gate against a scenario the system never
    faces.
    """
    raw = json.dumps(
        {
            "decision": "MATCH",
            "matched_settlement_ids": [
                base.candidates[0].settlement_id if base.candidates else "STL00001",
                _FABRICATED_ID,
            ],
            "evidence": [
                {
                    "field_name": "amount credited",
                    "bank_value": f"{base.credit_paise} paise",
                    "settlement_value": f"{base.credit_paise} paise",
                    "reasoning": (
                        "The first settlement accounts for the credit; the second covers "
                        "the residual fee."
                    ),
                }
            ],
            "confidence": 0.97,
        },
        indent=2,
    )
    return Adjudication(
        bank_txn_id=base.bank_txn_id,
        credit_paise=base.credit_paise,
        value_date=base.value_date,
        narration=base.narration,
        candidates=base.candidates,
        candidates_total=base.candidates_total,
        prompt=base.prompt,
        raw_response=f"```json\n{raw}\n```",
        stages=[
            GateStage("schema", True, ""),
            GateStage(
                "membership",
                False,
                f"response named {_FABRICATED_ID}, which was not in the candidate set; "
                "the whole response is discarded",
                (_FABRICATED_ID,),
            ),
        ],
        verdict="LLM_INVALID_OUTPUT",
        scripted=True,
    )


def _ablation(markdown: str | None, fixture: str) -> list[AblationRow]:
    """Parse this fixture's rows out of the generated summary table.

    Parsed rather than recomputed: `results/summary.md` is written by `make eval` and is
    the single source for every published figure (rule 7, ADR-036). Recomputing here would
    create a second source that could disagree with it.
    """
    if not markdown:
        return []
    rows: list[AblationRow] = []
    for line in markdown.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 6 or cells[0] != f"`{fixture}`":
            continue
        label = cells[1]
        if "not yet measured" in line:
            rows.append(AblationRow(label, 0.0, 0.0, 0.0, 0, 0, measured=False))
            continue

        def number(cell: str) -> float:
            match = re.search(r"([\d.]+)%", cell)
            return float(match.group(1)) / 100 if match else 0.0

        rows.append(
            AblationRow(
                label=label,
                auto_match_rate=number(cells[2]),
                precision=number(cells[3]),
                false_match_rate=number(cells[4]),
                adjudications=int(re.sub(r"\D", "", cells[5]) or 0),
                matches_incorrect=0,
            )
        )
    return rows


def assemble(
    conn: Connection,
    run_id: str,
    *,
    fixture: str,
    records: int,
    seed: int,
    metrics: object | None,
    cache_dir: Path,
    ablation_md: str | None = None,
    provenance_limit: int = 12,
    adjudication_limit: int = 3,
) -> ReportData:
    """Build the full report payload for one run."""
    header = conn.execute(
        text("SELECT tiers_enabled FROM runs WHERE run_id = :run"), {"run": run_id}
    ).scalar_one_or_none()
    ingested = conn.execute(
        text(
            "SELECT (SELECT COUNT(*) FROM bank_txns WHERE run_id = :run) + "
            "(SELECT COUNT(*) FROM settlements WHERE run_id = :run) + "
            "(SELECT COUNT(*) FROM invoices WHERE run_id = :run)"
        ),
        {"run": run_id},
    ).scalar_one()
    quarantined = conn.execute(
        text("SELECT COUNT(*) FROM quarantine WHERE run_id = :run"), {"run": run_id}
    ).scalar_one()
    used_model = conn.execute(
        text(
            "SELECT COUNT(*) FROM match_records "
            "WHERE run_id = :run AND tier = 3 AND model_name IS NOT NULL"
        ),
        {"run": run_id},
    ).scalar_one()

    steps, residual = _tiers(conn, run_id)

    adjudications: list[Adjudication] = []
    if used_model:
        with tempfile.TemporaryDirectory() as scratch:
            adjudications = replay_tier3(
                fixture=fixture,
                records=records,
                seed=seed,
                cache_dir=cache_dir,
                workdir=Path(scratch),
                limit=adjudication_limit,
            )
        if adjudications:
            adjudications.append(_scripted_hallucination(adjudications[0]))

    return ReportData(
        run=RunHeader(
            run_id=run_id,
            fixture=fixture,
            records=records,
            seed=seed,
            tiers=header or "",
            rows_ingested=ingested,
            rows_quarantined=quarantined,
            keyless=True,
        ),
        metrics=metrics,
        tiers=steps,
        residual=residual,
        clusters=_clusters(conn, run_id),
        provenance=_provenance(conn, run_id, provenance_limit),
        adjudications=adjudications,
        ablation_rows=_ablation(ablation_md, fixture),
    )
