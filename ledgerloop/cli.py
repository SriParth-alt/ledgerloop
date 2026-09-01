"""LedgerLoop command line — the matcher's own commands.

The CLI is the demo surface. `make demo` runs generate -> reconcile -> report, and
that sequence is what the pitch video shows. Keep the output legible on a recording:
streaming per-tier counts, then a metrics table.

`report` and `evaluate` are **not** here. They need ground truth to score against, and
this module sits inside the package that may never read it —
`tests/test_no_truth_leak.py` fails on any import of `eval` from here, and it is right
to. Those two commands live in `eval/cli.py`, which composes both halves into the
published entrypoint. The dependency runs one way: `eval` may know about the matcher,
never the reverse.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import typer
from rich.console import Console

from ledgerloop.cascade.orchestrator import TierOutcome, parse_tiers
from ledgerloop.cascade.orchestrator import reconcile as run_cascade
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.clustering import cluster, open_exceptions
from ledgerloop.generate.synth import (
    BANK_FILE,
    INVOICES_FILE,
    SETTLEMENTS_FILE,
    generate_fixture,
)
from ledgerloop.ingest.loader import load_batch
from ledgerloop.rules.promote import (
    attach_promoted_rule,
    promote,
    propose_rule,
    record_resolution,
)
from ledgerloop.store.db import DEFAULT_DB_PATH, connect, initialise, run_exists, start_run

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
    fixtures_dir: Path = typer.Option(Path("fixtures"), help="Where generate wrote its files."),
    db: Path = typer.Option(DEFAULT_DB_PATH, help="SQLite file to ingest into."),
) -> None:
    """Run the cascade over an ingested batch.

    Prints a live per-tier count as it goes — that streaming output is the moment in
    the demo where the architecture becomes visible.
    """
    try:
        selected = parse_tiers(tiers)
    except ValueError as error:
        console.print(f"[red]bad --tiers[/] {error}")
        raise typer.Exit(code=2) from None

    source = fixtures_dir / fixture
    with connect(db) as conn:
        initialise(conn)
        if run_exists(conn, run_id):
            console.print(f"[red]run already exists[/] {run_id!r} — choose another --run-id")
            raise typer.Exit(code=2)

        start_run(
            conn,
            run_id=run_id,
            fixture=fixture,
            tiers_enabled=",".join(str(tier) for tier in sorted(selected)),
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )

        reports = load_batch(
            conn,
            run_id,
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        loaded = sum(report.inserted for report in reports.values())
        quarantined = sum(report.quarantined for report in reports.values())
        console.print(f"[green]ingested[/] {loaded} rows, {quarantined} quarantined")

        def show(outcome: TierOutcome) -> None:
            console.print(
                f"  tier {outcome.tier}  "
                f"{outcome.matched_bank_txns:>5} credits  "
                f"{outcome.matched_settlements:>5} settlements"
            )

        report = run_cascade(conn, run_id, tiers=selected, no_llm=no_llm, on_tier=show)

    console.print(
        f"[bold]unmatched[/] {report.unmatched_bank_txns} credits, "
        f"{report.unmatched_settlements} settlements"
        + ("  [yellow](degraded run)[/]" if report.degraded else "")
    )


@app.command()
def exceptions(
    run_id: str = typer.Option(...),
    limit: int = typer.Option(20),
    db: Path = typer.Option(DEFAULT_DB_PATH),
) -> None:
    """List open exceptions, highest rupee value at risk first.

    The clustering comes first deliberately. Twelve exceptions sharing a reason code is
    one wrong assumption, not twelve problems, and an associate who reads the list before
    the pattern will work through twelve rows to discover that once.
    """
    with connect(db) as conn:
        if not run_exists(conn, run_id):
            console.print(f"[red]no such run[/] {run_id!r}")
            raise typer.Exit(code=2)
        items = open_exceptions(conn, run_id)

    if not items:
        console.print(f"[green]no open exceptions[/] for run {run_id}")
        return

    total = sum(item.value_at_risk_paise for item in items)
    console.print(
        f"[bold]{len(items)} open exception(s)[/], Rs {total / 100:,.2f} at risk"
    )
    console.print()

    console.print("[bold]Patterns[/]")
    for group in cluster(items):
        console.print(
            f"  [bold]{group.code.value}[/]  x{group.count}  "
            f"Rs {group.value_at_risk_paise / 100:,.2f}"
        )
        console.print(f"    {group.diagnosis}")

    console.print()
    console.print(f"[bold]Queue[/] — highest value at risk first (top {limit})")
    for item in items[:limit]:
        console.print(
            f"  Rs {item.value_at_risk_paise / 100:>13,.2f}  "
            f"{item.code.value:<24} {item.bank_txn_id or '-'}"
        )
        console.print(f"      {item.suggested_action}")


@app.command()
def resolve(
    run_id: str = typer.Option(..., help="Which run the exception belongs to."),
    exception_id: str = typer.Option(..., help="From `ledgerloop exceptions`."),
    settlement_id: str = typer.Option(..., help="The settlement that explains this credit."),
    resolved_by: str = typer.Option("analyst", help="Who decided."),
    approve: bool = typer.Option(False, help="Persist the proposed rule."),
    store: Path = typer.Option(Path("ledgerloop/rules/store.yaml")),
    db: Path = typer.Option(DEFAULT_DB_PATH),
) -> None:
    """Resolve an exception, and optionally approve the rule it generalises to.

    Approval is deliberately a second flag rather than implied. A resolution records what
    a human decided about one record; a rule fires forever on batches nobody has looked
    at. ADR-004 makes that approval the only gate between the two, and a command that did
    both at once would make the gate a formality.

    This is the surface that replaced the UI when day 12 was cut (ADR-030).
    """
    with connect(db) as conn:
        if not run_exists(conn, run_id):
            console.print(f"[red]no such run[/] {run_id!r}")
            raise typer.Exit(code=2)
        try:
            resolution = record_resolution(
                conn,
                run_id,
                exception_id,
                settlement_id=settlement_id,
                resolved_by=resolved_by,
            )
        except KeyError as error:
            console.print(f"[red]{error}[/]")
            raise typer.Exit(code=2) from None

        console.print(
            f"[green]resolved[/] {exception_id} -> {settlement_id} by {resolved_by}"
        )

        proposal = propose_rule(resolution)
        if proposal is None:
            console.print(
                "  no generalisable rule — this resolution looks like a one-off, and "
                "inventing a rule from it would fire on the next batch"
            )
            return

        console.print()
        console.print(f"[bold]Proposed rule[/] ({proposal.kind.value})")
        console.print(f"  {proposal.description}")
        console.print(f"  machine form: {proposal.value}")

        if not approve:
            console.print(
                "[yellow]not approved[/] — nothing was persisted. Re-run with "
                "--approve to promote it."
            )
            return

        promote(proposal, store, approved_by=resolved_by)
        attach_promoted_rule(conn, exception_id, proposal.value)
        console.print(f"[green]promoted[/] to {store} — it applies from the next run")


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
