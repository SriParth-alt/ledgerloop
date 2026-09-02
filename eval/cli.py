"""Evaluation commands, and the composition root for the published CLI.

`report` and `evaluate` live here rather than in `ledgerloop/cli.py` because both need
to read ground truth, and the matcher package may never do that. That is not a
convention — `tests/test_no_truth_leak.py` fails the build on any import of `eval` from
inside `ledgerloop/`, and it caught this exact mistake when the commands were first
written in the wrong place.

The dependency direction is the whole point: `eval` imports the matcher's CLI and adds
to it. The matcher never learns that an evaluator exists, so the capability to read
truth cannot drift sideways into it (ADR-023).
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import typer
from dotenv import load_dotenv

from eval.ablation import estimate_calls, run_ablation, write_report
from eval.harness import load_truth, score_run
from ledgerloop.cli import app, console
from ledgerloop.generate.synth import TRUTH_FILE
from ledgerloop.llm.adapter import GEMINI_DEFAULT_MODEL, RateLimit, build_adapter
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.store.db import DEFAULT_DB_PATH, connect, run_exists

#: Committed alongside the code so CI — and a reviewer with no key — can re-run Tier 3
#: for free and get byte-identical results. The cache is the reproducibility guarantee
#: (§7.4); temperature and seed only make the *first* call of a new prompt stable.
DEFAULT_CACHE_DIR = Path("fixtures/llm_cache")


@app.command()
def report(
    run_id: str = typer.Option(..., help="Which run to summarise."),
    fixture: str = typer.Option("realistic", help="Which fixture the run used."),
    fixtures_dir: Path = typer.Option(Path("fixtures")),
    db: Path = typer.Option(DEFAULT_DB_PATH),
) -> None:
    """Print the metrics table and exception breakdown for one run."""
    with connect(db) as conn:
        if not run_exists(conn, run_id):
            console.print(f"[red]no such run[/] {run_id!r}")
            raise typer.Exit(code=2)
        metrics = score_run(
            conn, run_id, load_truth(fixtures_dir / fixture / TRUTH_FILE), seconds=0.0
        )

    console.print(f"[bold]run[/] {run_id}  fixture {fixture}")
    console.print(
        f"  credits            {metrics.credits_total} "
        f"({metrics.credits_explainable} explainable)"
    )
    console.print(f"  auto-match rate    {metrics.auto_match_rate:.1%}")
    console.print(f"  precision          {metrics.precision:.1%}")
    console.print(f"  [bold]false-match rate   {metrics.false_match_rate:.1%}[/]")
    console.print(f"  recall             {metrics.recall:.1%}")
    console.print(
        f"  posted / wrong     {metrics.matches_posted} / {metrics.matches_incorrect}"
    )
    console.print(
        f"  value reconciled   Rs {metrics.value_reconciled_paise / 100:,.2f}  "
        f"at risk Rs {metrics.value_at_risk_paise / 100:,.2f}"
    )
    if metrics.exceptions_by_code:
        console.print("  exceptions")
        for code, count in sorted(metrics.exceptions_by_code.items()):
            console.print(f"    {code:<22} {count}")


@app.command()
def evaluate(
    all_fixtures: bool = typer.Option(False, "--all-fixtures"),
    fixture: str = typer.Option(
        "adversarial", help="Score one fixture. Ignored when --all-fixtures is set."
    ),
    out: Path = typer.Option(Path("results/metrics.md")),
    records: int = typer.Option(250, min=50),
    seed: int = typer.Option(42),
    no_llm: bool = typer.Option(False, "--no-llm", help="Skip the two model arms."),
    estimate_only: bool = typer.Option(
        False, "--estimate-only", help="Count the calls a sweep would make. Sends nothing."
    ),
    model: str = typer.Option(GEMINI_DEFAULT_MODEL, help="Pinned into the cache key."),
    cache_dir: Path = typer.Option(DEFAULT_CACHE_DIR),
    requests_per_minute: int = typer.Option(15, help="Provider ceiling; paces the sweep."),
    requests_per_day: int = typer.Option(500, help="Provider ceiling; stops the sweep."),
) -> None:
    """Run the full ablation and write results.

    Everything in README.md comes from this command. Never hand-write a metric.

    ``--estimate-only`` exists because the model arms cost real quota and the free tier
    allows fewer requests per day than a full sweep needs. It reports what would be sent
    without sending it, so the call count is approved before anything is spent.
    """
    # One fixture at a time is not a convenience. A full sweep needs more requests
    # than a free tier allows in a day, and running them in file order would spend
    # half the quota on `easy` — the fixture the deterministic tiers already solve
    # completely — before reaching the rows that carry the argument. Answers are
    # cached, so a later --all-fixtures pass assembles the table for free.
    fixtures = ("easy", "realistic", "adversarial") if all_fixtures else (fixture,)

    if estimate_only:
        total = 0
        with TemporaryDirectory() as scratch:
            for name in fixtures:
                estimate = estimate_calls(
                    name, records=records, seed=seed, workdir=Path(scratch) / name
                )
                total += estimate.total_calls
                console.print(
                    f"  {name:<12} full cascade {estimate.full_cascade_calls:>4}  "
                    f"LLM-only {estimate.llm_only_calls:>4}  "
                    f"(~{estimate.estimated_input_tokens:,} input tokens)"
                )
        console.print()
        console.print(
            f"[bold]{total}[/] calls against a limit of {requests_per_day}/day"
        )
        if total > requests_per_day:
            console.print(
                f"[yellow]{total - requests_per_day} call(s) will not fit today.[/] "
                "Cached answers persist, so re-running tomorrow resumes for free."
            )
        return

    adapter = None
    cache = None
    if not no_llm:
        cache = ResponseCache(cache_dir)
        adapter = build_adapter(
            model=model,
            limit=RateLimit(
                requests_per_minute=requests_per_minute, requests_per_day=requests_per_day
            ),
        )
        if adapter is None:
            console.print(
                "[yellow]no API key found[/] — the model arms will report "
                "'not yet measured'. Set GEMINI_API_KEY or GOOGLE_API_KEY."
            )

    results = {}
    with TemporaryDirectory() as scratch:
        for name in fixtures:
            console.print(f"[cyan]scoring[/] {name} ({records} records, seed {seed})")
            results[name] = run_ablation(
                name,
                records=records,
                seed=seed,
                workdir=Path(scratch) / name,
                adapter=adapter,
                cache=cache,
            )
            for arm in results[name]:
                if arm.metrics is None:
                    detail = f" ({arm.detail})" if arm.detail else ""
                    console.print(f"  {arm.label:<20} [yellow]not yet measured[/]{detail}")
                    continue
                console.print(
                    f"  {arm.label:<20} auto {arm.metrics.auto_match_rate:>6.1%}  "
                    f"precision {arm.metrics.precision:>6.1%}  "
                    f"false-match {arm.metrics.false_match_rate:>6.1%}"
                )
        write_report(results, out)

    console.print(f"[green]wrote[/] {out}")


def main() -> None:
    """Entrypoint for the `ledgerloop` script: matcher commands plus evaluation."""
    # A key in .env should behave exactly like a key in the environment. Loaded here,
    # at the composition root, rather than inside the adapter: a library that reads
    # files off disk at import time is a library that surprises its callers.
    load_dotenv()
    app()
