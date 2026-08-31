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

from eval.ablation import run_ablation, write_report
from eval.harness import load_truth, score_run
from ledgerloop.cli import app, console
from ledgerloop.generate.synth import TRUTH_FILE
from ledgerloop.store.db import DEFAULT_DB_PATH, connect, run_exists


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
    out: Path = typer.Option(Path("results/metrics.md")),
    records: int = typer.Option(250, min=50),
    seed: int = typer.Option(42),
) -> None:
    """Run the full ablation and write results.

    Everything in README.md comes from this command. Never hand-write a metric.
    """
    fixtures = ("easy", "realistic", "adversarial") if all_fixtures else ("adversarial",)
    results = {}
    with TemporaryDirectory() as scratch:
        for name in fixtures:
            console.print(f"[cyan]scoring[/] {name} ({records} records, seed {seed})")
            results[name] = run_ablation(
                name, records=records, seed=seed, workdir=Path(scratch) / name
            )
            for arm in results[name]:
                if arm.metrics is None:
                    console.print(f"  {arm.label:<20} [yellow]not yet measured[/]")
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
    app()
