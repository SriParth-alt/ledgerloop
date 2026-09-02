"""Replay Tier 3 from the committed cache, capturing every stage.

This is what makes the report a demonstration rather than a summary. For each credit that
reached Tier 3 it recovers the candidate pool, the exact prompt, the raw text the model
returned, and each gate's verdict on that text — so a viewer sees the mechanism, not just
the outcome.

**Replayed, never re-requested.** Every response comes from `fixtures/llm_cache`, so the
demo cannot rate-limit, time out, or answer differently on stage. Both of those failures
happened during the real sweeps: a 503 killed a run 58 calls in, and a daily quota ran out
mid-fixture (ADR-031, ADR-032). A live call during a recorded pitch is a risk with no
upside, because the cached answer is the same answer.

Nothing here reads ground truth. The replay is built from the run and the cache alone,
which is what lets it live in `ledgerloop/` at all (rule 6).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy import text

from ledgerloop.cascade.gates import (
    arithmetic_gate,
    confidence_gate,
    membership_gate,
    schema_gate,
)
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.cascade.tier3_llm import rank_candidates
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL
from ledgerloop.generate.synth import (
    BANK_FILE,
    INVOICES_FILE,
    SETTLEMENTS_FILE,
    generate_fixture,
)
from ledgerloop.ingest.loader import load_batch
from ledgerloop.llm.adapter import DEFAULT_MODEL
from ledgerloop.llm.cache import ResponseCache, cache_key
from ledgerloop.llm.prompts.v1 import render
from ledgerloop.report.model import Adjudication, CandidateRow, GateStage
from ledgerloop.store.db import (
    connect,
    initialise,
    load_bank_txns,
    load_settlements,
    start_run,
)

POSTED = "POSTED"

#: One case per outcome, in this order. A demo that showed only clean matches would prove
#: nothing about the gates — the rejections are the evidence — so the selection reaches
#: for one of each rather than the first N it happens to find.
PREFERRED_ORDER = (
    POSTED,
    ExceptionCode.AMOUNT_BEYOND_TOLERANCE.value,
    ExceptionCode.LLM_INVALID_OUTPUT.value,
)


def _run_gates(raw: str, txn: object, by_id: dict[str, object]) -> tuple[list[GateStage], str]:
    """Run the gates in the same order Tier 3 does, recording each verdict.

    Deliberately re-running them rather than reading the stored exception: the point is to
    show the checks happening, and a recorded outcome cannot show *which* gate stopped it
    or what it said.
    """
    stages: list[GateStage] = []

    parsed = schema_gate(raw)
    stages.append(GateStage("schema", parsed.accepted, parsed.detail))
    if not parsed.accepted or parsed.adjudication is None:
        return stages, ExceptionCode.LLM_INVALID_OUTPUT.value

    checked = membership_gate(parsed.adjudication, frozenset(by_id))
    stages.append(
        GateStage("membership", checked.accepted, checked.detail, checked.hallucinated_ids)
    )
    if not checked.accepted:
        return stages, ExceptionCode.LLM_INVALID_OUTPUT.value

    computed = arithmetic_gate(
        parsed.adjudication,
        txn.credit_paise,  # type: ignore[attr-defined]
        txn.value_date,  # type: ignore[attr-defined]
        by_id,  # type: ignore[arg-type]
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )
    stages.append(GateStage("arithmetic", computed.accepted, computed.detail))
    if not computed.accepted:
        return stages, ExceptionCode.AMOUNT_BEYOND_TOLERANCE.value

    confident = confidence_gate(parsed.adjudication)
    stages.append(GateStage("confidence", confident.accepted, confident.detail))
    return stages, POSTED if confident.accepted else ExceptionCode.LOW_CONFIDENCE.value


def replay_tier3(
    *,
    fixture: str,
    records: int,
    seed: int,
    cache_dir: Path,
    workdir: Path,
    limit: int = 4,
    model_name: str = DEFAULT_MODEL,
) -> list[Adjudication]:
    """Rebuild the fixture, run T0-T2, and replay Tier 3 over what is left.

    Deterministic: the fixture is regenerated from the seed, the deterministic tiers are
    replayed, and every prompt is answered from cache. Two calls with the same arguments
    produce the same cases in the same order (§7.4).
    """
    workdir = Path(workdir)
    cache = ResponseCache(Path(cache_dir))
    generate_fixture(fixture=fixture, settlements=records, seed=seed, out_dir=workdir / "fx")
    source = workdir / "fx" / fixture

    with connect(workdir / "replay.db") as conn:
        initialise(conn)
        start_run(
            conn,
            run_id="replay",
            fixture=fixture,
            tiers_enabled="0,1,2",
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )
        load_batch(
            conn,
            "replay",
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        reconcile(conn, "replay", tiers=frozenset({0, 1, 2}))
        claimed = {
            row[0]
            for row in conn.execute(
                text("SELECT bank_txn_id FROM match_records WHERE run_id = 'replay'")
            )
        }
        settlements = load_settlements(conn, "replay")
        residual = [b for b in load_bank_txns(conn, "replay") if b.bank_txn_id not in claimed]

    found: list[Adjudication] = []
    for txn in residual:
        candidates = rank_candidates(
            txn, settlements, config=DEFAULT_MATCH_CONFIG, fee_model=SETTLEMENT_FEE_MODEL
        )
        if not candidates:
            continue
        prompt = render(
            txn,
            candidates,
            fee_model=SETTLEMENT_FEE_MODEL,
            slack_days=DEFAULT_MATCH_CONFIG.settlement_slack_days,
        )
        raw = cache.get(cache_key(prompt, model=model_name))
        if raw is None:
            continue

        by_id = {row.settlement_id: row for row in candidates}
        stages, verdict = _run_gates(raw, txn, by_id)
        found.append(
            Adjudication(
                bank_txn_id=txn.bank_txn_id,
                credit_paise=txn.credit_paise,
                value_date=str(txn.value_date),
                narration=txn.narration,
                candidates=[
                    CandidateRow(c.settlement_id, c.customer_name, c.gross_amount_paise)
                    for c in candidates[:5]
                ],
                candidates_total=len(candidates),
                prompt=prompt,
                raw_response=raw,
                stages=stages,
                verdict=verdict,
            )
        )

    return _select(found, limit)


def _select(cases: list[Adjudication], limit: int) -> list[Adjudication]:
    """One case per outcome first, then whatever else fits.

    Taking the first N in file order would very likely return N successes, and a demo of
    gates that never reject demonstrates nothing.
    """
    chosen: list[Adjudication] = []
    seen: set[str] = set()
    for verdict in PREFERRED_ORDER:
        for case in cases:
            if case.verdict == verdict and verdict not in seen:
                chosen.append(case)
                seen.add(verdict)
                break
    for case in cases:
        if len(chosen) >= limit:
            break
        if case not in chosen:
            chosen.append(case)
    return chosen[:limit]
