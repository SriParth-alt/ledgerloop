"""Runs the same fixture under progressively enabled tiers.

T0 / T0+T1 / T0+T1+T2 / full / LLM-ONLY BASELINE.

The LLM-only baseline is not optional. It is the control arm that converts
'I built a cascade' into 'I measured that the cascade beats the obvious approach
on the metric that matters, using fewer model calls'. Without it the
architecture is an opinion.

Writes results/metrics.md. Never hand-edit that file.

**The model arms are all-or-nothing.** The free tier allows 500 requests a day and a
full sweep needs 677, so an interrupted run is the normal case rather than the
exceptional one. The tempting behaviour — score whatever came back, footnote the gap —
would put a figure in the table computed over the fraction of a fixture that happened to
fit inside a quota window: unreproducible, and quoted by everyone who read it. An arm
that could not ask about every credit reports *not yet measured*, exactly as it did while
Tier 3 was unimplemented. Rule 7 is as much about not implying a number as not typing
one.

Interruption is cheap because the cache is written per response. Every answer already
paid for survives, and tomorrow's attempt resumes without re-buying it.

Every arm runs the same fixture at the same seed and differs only in which tiers
executed. Regenerating the fixture per arm would make the table a comparison of datasets
rather than of tiers.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from sqlalchemy import Connection, text

from eval.harness import Truth, load_truth, score_run
from eval.metrics import RunMetrics
from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.cascade.tier3_llm import rank_candidates
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL
from ledgerloop.generate.synth import BANK_FILE, INVOICES_FILE, SETTLEMENTS_FILE, generate_fixture
from ledgerloop.ingest.loader import load_batch
from ledgerloop.llm.adapter import LLMAdapter, SweepInterruptedError
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.store.db import connect, initialise, load_bank_txns, load_settlements, start_run

NOT_MEASURED = "not yet measured"

#: Why an arm has no number. Distinguishing "never attempted" from "attempted and the
#: quota ran out" matters to whoever picks the run back up tomorrow.
NO_MODEL = "no model configured"
INTERRUPTED = "sweep interrupted (quota or provider outage) before every credit was asked about"


@dataclass(frozen=True)
class AblationArm:
    """One row of §9.2. ``metrics`` is None when the arm could not be measured."""

    label: str
    tiers: frozenset[int]
    metrics: RunMetrics | None = None
    note: str = ""
    detail: str = ""


#: The progression §9.2 fixes. Pinned as data rather than written inline so a row cannot
#: quietly change meaning between two published tables.
DETERMINISTIC_ARMS: tuple[AblationArm, ...] = (
    AblationArm(label="T0 only", tiers=frozenset({0})),
    AblationArm(label="T0 + T1", tiers=frozenset({0, 1})),
    AblationArm(label="T0 + T1 + T2", tiers=frozenset({0, 1, 2})),
)

#: The rows that need a model. Present in the table whether or not they ran, so their
#: absence is visible rather than silent.
MODEL_ARMS: tuple[AblationArm, ...] = (
    AblationArm(label="Full cascade", tiers=frozenset({0, 1, 2, 3})),
    AblationArm(label="LLM-only baseline", tiers=frozenset({3})),
)

#: Retained under its old name: `PENDING_ARMS` was what these rows were called while
#: Tier 3 did not exist.
PENDING_ARMS = MODEL_ARMS


@dataclass(frozen=True)
class CallEstimate:
    """What a sweep would cost, computed without sending anything.

    Reported per arm rather than as one total, because the two differ by an order of
    magnitude — the full cascade only asks about the residual the deterministic tiers
    could not resolve, while the baseline asks about every credit. When a daily quota is
    short, that difference is the whole decision about what to cut.
    """

    fixture: str
    full_cascade_calls: int
    llm_only_calls: int
    estimated_input_tokens: int

    @property
    def total_calls(self) -> int:
        return self.full_cascade_calls + self.llm_only_calls


def _prepare(workdir: Path, fixture: str, records: int, seed: int) -> tuple[Path, Truth]:
    paths = generate_fixture(
        fixture=fixture, settlements=records, seed=seed, out_dir=workdir / "fixtures"
    )
    return workdir / "fixtures" / fixture, load_truth(paths["truth"])


def _load(conn: Connection, run_id: str, source: Path, fixture: str) -> None:
    initialise(conn)
    start_run(
        conn,
        run_id=run_id,
        fixture=fixture,
        tiers_enabled="pending",
        config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
    )
    load_batch(
        conn,
        run_id,
        invoices=source / INVOICES_FILE,
        settlements=source / SETTLEMENTS_FILE,
        bank_statement=source / BANK_FILE,
    )


def _tier3_prompt_count(conn: Connection, run_id: str) -> tuple[int, int]:
    """How many credits Tier 3 would actually ask about, and their prompt size.

    Not simply the residual: a credit with no ranked candidate is skipped, because
    calling a model with an empty list spends a request to be told nothing and invites it
    to invent one. Counting the residual instead would overstate the bill.
    """
    from ledgerloop.llm.prompts.v1 import render

    matched = {
        row[0]
        for row in conn.execute(
            text("SELECT bank_txn_id FROM match_records WHERE run_id = :run"),
            {"run": run_id},
        )
    }
    settlements = load_settlements(conn, run_id)
    calls = 0
    characters = 0
    for bank_txn in load_bank_txns(conn, run_id):
        if bank_txn.bank_txn_id in matched:
            continue
        candidates = rank_candidates(
            bank_txn, settlements, config=DEFAULT_MATCH_CONFIG, fee_model=SETTLEMENT_FEE_MODEL
        )
        if not candidates:
            continue
        calls += 1
        characters += len(
            render(
                bank_txn,
                candidates,
                fee_model=SETTLEMENT_FEE_MODEL,
                slack_days=DEFAULT_MATCH_CONFIG.settlement_slack_days,
            )
        )
    # Four characters to the token is the conventional rough conversion. Deliberately an
    # estimate: the real figure is read from the provider once calls are actually made.
    return calls, characters // 4


def estimate_calls(
    fixture: str,
    *,
    records: int,
    seed: int,
    workdir: Path,
    adapter: LLMAdapter | None = None,
) -> CallEstimate:
    """Count the requests a sweep would make. Sends nothing.

    ``adapter`` is accepted so a caller can hand over the very adapter it intends to run
    with — and the fact that this function never invokes it is the guarantee being
    offered. A user approves a call count before any quota is spent.
    """
    del adapter  # deliberately never called; see the docstring
    workdir = Path(workdir)
    source, _ = _prepare(workdir, fixture, records, seed)

    with connect(workdir / "estimate-full.db") as conn:
        _load(conn, "estimate-full", source, fixture)
        reconcile(conn, "estimate-full", tiers=frozenset({0, 1, 2}))
        full_calls, full_tokens = _tier3_prompt_count(conn, "estimate-full")

    with connect(workdir / "estimate-only.db") as conn:
        _load(conn, "estimate-only", source, fixture)
        only_calls, only_tokens = _tier3_prompt_count(conn, "estimate-only")

    return CallEstimate(
        fixture=fixture,
        full_cascade_calls=full_calls,
        llm_only_calls=only_calls,
        estimated_input_tokens=full_tokens + only_tokens,
    )


def _score_arm(
    arm: AblationArm,
    workdir: Path,
    source: Path,
    fixture: str,
    truth: Truth,
    *,
    adapter: LLMAdapter | None,
    cache: ResponseCache | None,
) -> AblationArm:
    """Run one arm, or report honestly why it has no number."""
    run_id = arm.label.replace(" ", "").replace("+", "_")
    needs_model = 3 in arm.tiers

    if needs_model and adapter is None:
        return AblationArm(label=arm.label, tiers=arm.tiers, note=NOT_MEASURED, detail=NO_MODEL)

    with connect(workdir / f"{fixture}-{run_id}.db") as conn:
        _load(conn, run_id, source, fixture)

        # Timed around reconciliation only. Including generation and ingest would
        # measure the fixture rather than the matcher.
        started = time.perf_counter()
        try:
            result = reconcile(
                conn, run_id, tiers=arm.tiers, adapter=adapter, cache=cache
            )
        except SweepInterruptedError:
            # Every answer bought so far is already on disk. Refusing to score is the
            # point: a rate over the part of the fixture that fit inside a quota window
            # is not a measurement of anything.
            return AblationArm(
                label=arm.label, tiers=arm.tiers, note=NOT_MEASURED, detail=INTERRUPTED
            )
        elapsed = time.perf_counter() - started

        return AblationArm(
            label=arm.label,
            tiers=arm.tiers,
            metrics=score_run(
                conn,
                run_id,
                truth,
                seconds=elapsed,
                llm_invocations=result.llm_invocations,
                cache_hits=result.cache_hits,
                hallucinations=result.hallucinations,
            ),
        )


def run_ablation(
    fixture: str,
    *,
    records: int,
    seed: int,
    workdir: Path,
    adapter: LLMAdapter | None = None,
    cache: ResponseCache | None = None,
) -> list[AblationArm]:
    """Score every arm of §9.2 against one generated fixture."""
    workdir = Path(workdir)
    source, truth = _prepare(workdir, fixture, records, seed)

    return [
        _score_arm(arm, workdir, source, fixture, truth, adapter=adapter, cache=cache)
        for arm in (*DETERMINISTIC_ARMS, *MODEL_ARMS)
    ]


def write_report(arms: dict[str, list[AblationArm]], out: Path) -> None:
    """Write ``results/metrics.md``.

    Every figure here came from a scored run. Regenerating the file from the same arms
    produces the same bytes, which is what makes rule 7 checkable rather than a promise.
    """
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)

    every_arm = [arm for results in arms.values() for arm in results]
    unmeasured = [arm for arm in every_arm if arm.metrics is None]

    lines = [
        "# LedgerLoop — measured results",
        "",
        "> Generated by `ledgerloop evaluate`. Never hand-edited. Every figure below came",
        "> from scoring a run against the generator's ground truth; a row marked",
        f"> *{NOT_MEASURED}* has no number of any kind, which is different from zero.",
        "",
        "## Configuration in force",
        "",
        "§9.1 requires reporting the MatchConfig alongside any metric. A tolerance-dependent",
        "number whose tolerance is unknown is not reproducible — and Tier 2's band alone was",
        "the difference between 4 matches and 41 (ADR-019).",
        "",
        "| Setting | Value |",
        "|---|---|",
    ]
    for key, value in sorted(asdict(DEFAULT_MATCH_CONFIG).items()):
        lines.append(f"| `{key}` | {value} |")

    for fixture, results in arms.items():
        seed_note = next((arm for arm in results if arm.metrics is not None), None)
        lines += [
            "",
            f"## Fixture: `{fixture}` (seed 42)",
            "",
            "| Configuration | Auto-match | Precision | False-match | Recall | Posted | Wrong |",
            "|---|---|---|---|---|---|---|",
        ]
        for arm in results:
            if arm.metrics is None:
                lines.append(
                    f"| {arm.label} | *{arm.note}* | *{arm.note}* | *{arm.note}* "
                    f"| *{arm.note}* | — | — |"
                )
                continue
            m = arm.metrics
            lines.append(
                f"| {arm.label} | {m.auto_match_rate:.1%} | {m.precision:.1%} "
                f"| **{m.false_match_rate:.1%}** | {m.recall:.1%} "
                f"| {m.matches_posted} | {m.matches_incorrect} |"
            )

        if seed_note is not None and seed_note.metrics is not None:
            lines += [
                "",
                f"Credits in fixture: {seed_note.metrics.credits_total} "
                f"({seed_note.metrics.credits_explainable} explainable; the remainder are "
                "orphans and re-posts that no matcher should resolve).",
            ]

        lines += [
            "",
            "### Exceptions raised, by reason code",
            "",
            "| Configuration | Codes |",
            "|---|---|",
        ]
        for arm in results:
            if arm.metrics is None:
                lines.append(f"| {arm.label} | *{arm.note}* |")
                continue
            spread = arm.metrics.exceptions_by_code
            rendered = ", ".join(f"{code} {count}" for code, count in sorted(spread.items()))
            lines.append(f"| {arm.label} | {rendered or '—'} |")

        model_rows = [a for a in results if 3 in a.tiers and a.metrics is not None]
        if model_rows:
            lines += [
                "",
                "### Model usage",
                "",
                "**Adjudications** is how many credits the configuration put to the model,",
                "and it is the figure §9.2 compares. **New API calls** is what this",
                "particular run paid for — a property of the response cache rather than of",
                "the architecture, and it falls to zero on a re-run. Reporting only the",
                "second would understate model usage by whatever the cache happened to",
                "hold.",
                "",
                "| Configuration | Adjudications | New API calls | Hallucinated ids |",
                "|---|---|---|---|",
            ]
            for arm in model_rows:
                assert arm.metrics is not None
                lines.append(
                    f"| {arm.label} | {arm.metrics.adjudications} "
                    f"| {arm.metrics.llm_invocations} "
                    f"| {arm.metrics.hallucinations} |"
                )

    if unmeasured:
        reasons = sorted({arm.detail for arm in unmeasured if arm.detail})
        lines += [
            "",
            "## Not yet measured",
            "",
            f"{len(unmeasured)} arm(s) carry no number. "
            + (f"Reason: {'; '.join(reasons)}. " if reasons else "")
            + "A partially answered arm is reported as unmeasured rather than scored over",
            "the fraction of the fixture that fit inside a quota window — that figure would",
            "be unreproducible and would still get quoted. Answers already paid for are",
            "cached, so resuming costs nothing for them.",
            "",
        ]
    else:
        lines += [
            "",
            "## Coverage",
            "",
            "Every arm of §9.2 carries a measured number, including the LLM-only control.",
            "",
        ]
    out.write_text("\n".join(lines), encoding="utf-8", newline="\n")
