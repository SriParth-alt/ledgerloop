"""LedgerLoop command line.

The CLI is the demo surface. `make demo` runs generate -> reconcile -> report, and
that sequence is what the pitch video shows. Keep the output legible on a recording:
streaming per-tier counts, then a metrics table.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ledgerloop.generate.synth import generate_fixture

app = typer.Typer(
    name="ledgerloop",
    help="Deterministic-first settlement reconciliation.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


@app.command()
def generate(
    fixture: str = typer.Option("realistic", help="easy | realistic | adversarial"),
    records: int = typer.Option(250, min=50, help="Track bar requires 50+."),
    seed: int = typer.Option(42, help="Same seed must reproduce byte-identical files."),
    out: Path = typer.Option(Path("fixtures"), help="Output directory."),
) -> None:
    """Generate a synthetic three-source batch plus hidden ground truth.

    The output paths are reported by iterating what the generator returns rather than
    naming the files here. That is not stylistic: this module sits outside the one
    package permitted to speak about ground truth, and tests/test_no_truth_leak.py
    enforces the boundary by scanning for the tokens a literal would introduce.
    """
    try:
        paths = generate_fixture(fixture=fixture, settlements=records, seed=seed, out_dir=out)
    except KeyError:
        console.print(
            f"[red]unknown fixture[/] {fixture!r} — expected easy, realistic or adversarial"
        )
        raise typer.Exit(code=2) from None

    console.print(
        f"[green]generated[/] {records} settlements — fixture [bold]{fixture}[/], seed {seed}"
    )
    for role, path in sorted(paths.items()):
        console.print(f"  {role:<16} {path}")


@app.command()
def reconcile(
    run_id: str = typer.Option(..., help="Names this run for later comparison."),
    fixture: str = typer.Option("realistic"),
    tiers: str = typer.Option("0,1,2,3", help="Ablation: e.g. '0,1' runs deterministic only."),
    no_llm: bool = typer.Option(False, help="Force a degraded run without Tier 3."),
) -> None:
    """Run the cascade over an ingested batch.

    TODO(day-4 onward): wire to ledgerloop.cascade.orchestrator.

    Print a live per-tier count as it goes — that streaming output is the moment in
    the demo where the architecture becomes visible.
    """
    console.print(f"[yellow]not implemented[/] — reconcile run={run_id} tiers={tiers}")
    raise typer.Exit(code=1)


@app.command()
def report(
    run_id: str = typer.Option(..., help="Which run to summarise."),
) -> None:
    """Print the metrics table and exception breakdown for one run.

    TODO(day-8): wire to eval.metrics.
    """
    console.print(f"[yellow]not implemented[/] — report run={run_id}")
    raise typer.Exit(code=1)


@app.command()
def evaluate(
    all_fixtures: bool = typer.Option(False, "--all-fixtures"),
    out: Path = typer.Option(Path("results/metrics.md")),
) -> None:
    """Run the full ablation and write results.

    TODO(day-8): wire to eval.ablation. Must include the LLM-only baseline — it is
    the control arm that makes every other number mean something.

    Everything in README.md comes from this command. Never hand-write a metric.
    """
    console.print(f"[yellow]not implemented[/] — evaluate -> {out}")
    raise typer.Exit(code=1)


@app.command()
def exceptions(
    run_id: str = typer.Option(...),
    limit: int = typer.Option(20),
) -> None:
    """List open exceptions, highest rupee value at risk first.

    TODO(day-10): wire to ledgerloop.exceptions.clustering. Sort by value, and show
    the reason-code clustering — twelve exceptions sharing a code and a merchant is
    one wrong assumption, not twelve problems.
    """
    console.print(f"[yellow]not implemented[/] — exceptions run={run_id} limit={limit}")
    raise typer.Exit(code=1)


@app.command()
def serve(
    port: int = typer.Option(8000),
) -> None:
    """Start the FastAPI exception-queue backend.

    TODO(day-12): wire to ledgerloop.api.main.
    """
    console.print(f"[yellow]not implemented[/] — serve on :{port}")
    raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
