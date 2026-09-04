"""`make demo` must work on a fresh clone with no API key.

This is §12's stated bar for day 13 — "fresh clone → `make demo` works" — and it is the
command the pitch video runs. It is also the most embarrassing thing that can break,
because it breaks in front of an audience rather than in CI.

The demo runs the **full cascade**, Tier 3 included, entirely from the committed response
cache. That is a deliberate choice over `--no-llm`: it shows the whole architecture
working, needs no key, costs nothing, and produces byte-identical output on every machine.
It only holds because three things line up exactly — the fixture, records and seed the
Makefile passes (adversarial, 250, 42), `reconcile` running without a rule store, and the
cache having been populated by an eval run with those same parameters. Any one of them
drifting turns Tier 3 in the demo into a wall of MODEL_UNAVAILABLE, silently, on a laptop
with no key.

`adversarial` is the demo fixture because on `realistic` Tier 3 correctly declines every
residual credit, so the tier the pitch is about reads as inert. Here it contributes real
matches and the gates are visibly rejecting proposals.

Nothing here calls a model, and nothing here constructs an adapter — that is the point.
A judge runs `make demo` with no key, so `adapter=None` is the configuration under test.
`llm_invocations == 0` plus `cache_hits > 0` is the assertion that the committed cache
actually carried the run.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from ledgerloop.cascade.orchestrator import reconcile
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.synth import (
    BANK_FILE,
    INVOICES_FILE,
    SETTLEMENTS_FILE,
    generate_fixture,
)
from ledgerloop.ingest.loader import load_batch
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.store.db import connect, initialise, start_run

REPO_ROOT = Path(__file__).resolve().parent.parent
COMMITTED_CACHE = REPO_ROOT / "fixtures" / "llm_cache"

# Exactly what `make demo` runs. Duplicated as constants rather than parsed out of the
# Makefile: if the Makefile changes, this test should fail and make someone re-check the
# cache, not quietly follow along.
DEMO_FIXTURE = "adversarial"
DEMO_RECORDS = 250
DEMO_SEED = 42
DEMO_TIERS = frozenset({0, 1, 2, 3})


def _run_demo(tmp_path: Path) -> tuple[object, object]:
    generate_fixture(
        fixture=DEMO_FIXTURE, settlements=DEMO_RECORDS, seed=DEMO_SEED, out_dir=tmp_path / "fx"
    )
    source = tmp_path / "fx" / DEMO_FIXTURE

    with connect(tmp_path / "demo.db") as conn:
        initialise(conn)
        start_run(
            conn,
            run_id="demo",
            fixture=DEMO_FIXTURE,
            tiers_enabled=",".join(str(t) for t in sorted(DEMO_TIERS)),
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )
        load_batch(
            conn,
            "demo",
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        result = reconcile(
            conn,
            "demo",
            tiers=DEMO_TIERS,
            adapter=None,
            cache=ResponseCache(COMMITTED_CACHE),
        )
        codes = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT reason_code FROM exceptions WHERE run_id = 'demo'"
            )
        }
    return result, codes


def test_the_demo_runs_tier_three_without_making_a_single_call(tmp_path: Path) -> None:
    """The committed cache must cover every prompt the demo generates.

    With no adapter, a cache miss cannot be filled — it becomes MODEL_UNAVAILABLE. So
    `cache_hits > 0` is the assertion that the committed responses actually carried the
    run, rather than Tier 3 quietly doing nothing.
    """
    result, _ = _run_demo(tmp_path)

    assert result.llm_invocations == 0
    assert result.cache_hits > 0, "the demo never reached Tier 3 — check tier selection"


def test_the_demo_never_reports_the_model_as_unavailable(tmp_path: Path) -> None:
    """The failure this test exists to catch, and it is silent.

    If the cache misses, Tier 3 raises MODEL_UNAVAILABLE for every residual credit and the
    run still *succeeds* — §8 requires exactly that graceful degradation. The demo would
    complete, the numbers would look plausible, and the tier the pitch is about would have
    done nothing at all.
    """
    _, codes = _run_demo(tmp_path)

    assert ExceptionCode.MODEL_UNAVAILABLE.value not in codes


def test_the_demo_is_byte_identical_between_runs(tmp_path: Path) -> None:
    """§7.4. Two runs of the same batch must agree about which money was matched, or the
    provenance record is not worth keeping."""
    first, _ = _run_demo(tmp_path / "a")
    second, _ = _run_demo(tmp_path / "b")

    assert first.unmatched_bank_txns == second.unmatched_bank_txns
    assert first.cache_hits == second.cache_hits


def test_the_demo_works_with_no_adapter_at_all(tmp_path: Path) -> None:
    """The judge's machine: a fresh clone, no key, no adapter constructed.

    This is the scenario ADR-026 promised and the one that was actually broken. The cache
    key used to be read off the live adapter's ``name``, so with no adapter it was computed
    under the empty string and missed all 559 committed responses — the cache that exists
    to make Tier 3 reproducible without a key only worked for people who had one.
    """
    generate_fixture(
        fixture=DEMO_FIXTURE, settlements=DEMO_RECORDS, seed=DEMO_SEED, out_dir=tmp_path / "fx"
    )
    source = tmp_path / "fx" / DEMO_FIXTURE

    with connect(tmp_path / "demo.db") as conn:
        initialise(conn)
        start_run(
            conn,
            run_id="demo",
            fixture=DEMO_FIXTURE,
            tiers_enabled="0,1,2,3",
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )
        load_batch(
            conn,
            "demo",
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        result = reconcile(
            conn,
            "demo",
            tiers=DEMO_TIERS,
            adapter=None,
            cache=ResponseCache(COMMITTED_CACHE),
        )
        codes = {
            row[0]
            for row in conn.exec_driver_sql(
                "SELECT reason_code FROM exceptions WHERE run_id = 'demo'"
            )
        }

    assert result.cache_hits > 0
    assert result.llm_invocations == 0
    assert ExceptionCode.MODEL_UNAVAILABLE.value not in codes


def test_provenance_records_which_model_actually_decided(tmp_path: Path) -> None:
    """Every Tier 3 match must name the model and prompt version that produced it.

    §7.4 requires a prompt change to be visible in the trail rather than inferred from
    whatever the code says later, and CLAUDE.md states the convention outright.

    **This test previously passed while the column was entirely NULL.** It filtered
    `WHERE model_name IS NOT NULL`, so an all-null column produced an empty set and the
    assertion held vacuously — the orchestrator had been calling `record_match` without
    model or prompt since Tier 3 landed. Assert the count first, then the contents; a
    filter that can empty the set is a filter that can hide the bug.
    """
    generate_fixture(
        fixture=DEMO_FIXTURE, settlements=DEMO_RECORDS, seed=DEMO_SEED, out_dir=tmp_path / "fx"
    )
    source = tmp_path / "fx" / DEMO_FIXTURE

    with connect(tmp_path / "demo.db") as conn:
        initialise(conn)
        start_run(
            conn,
            run_id="demo",
            fixture=DEMO_FIXTURE,
            tiers_enabled="0,1,2,3",
            config_json=json.dumps(asdict(DEFAULT_MATCH_CONFIG)),
        )
        load_batch(
            conn,
            "demo",
            invoices=source / INVOICES_FILE,
            settlements=source / SETTLEMENTS_FILE,
            bank_statement=source / BANK_FILE,
        )
        reconcile(
            conn, "demo", tiers=DEMO_TIERS, adapter=None,
            cache=ResponseCache(COMMITTED_CACHE),
        )
        rows = list(
            conn.exec_driver_sql(
                "SELECT model_name, prompt_version FROM match_records "
                "WHERE run_id = 'demo' AND tier = 3"
            )
        )
        lower_tiers = list(
            conn.exec_driver_sql(
                "SELECT model_name FROM match_records "
                "WHERE run_id = 'demo' AND tier < 3 AND model_name IS NOT NULL"
            )
        )

    assert rows, "no Tier 3 matches to check — the fixture or cache changed"
    for model_name, prompt_version in rows:
        assert model_name, "a Tier 3 match recorded no model"
        assert prompt_version, "a Tier 3 match recorded no prompt version"

    # And the converse: tiers 0-2 never call a model, so claiming one would be worse
    # than recording none.
    assert not lower_tiers


def test_the_windows_script_runs_the_same_steps_as_make() -> None:
    """`demo.ps1` exists because Windows has no `make` and `pip install -e .` leaves the
    console script off `PATH`. The guide's bar is "works on ANY machine that follows your
    setup instructions", and a judge on Windows hits both walls immediately.

    Two scripts describing one demo will drift. This pins them together: whatever the
    Makefile runs, the PowerShell script must run too.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    script = (REPO_ROOT / "demo.ps1").read_text(encoding="utf-8")

    demo = makefile.split("demo:", 1)[1].split("\n\n", 1)[0]
    for line in demo.splitlines():
        line = line.strip()
        if not line.startswith("ledgerloop "):
            continue
        # Compare the command and its arguments, ignoring the whitespace the script uses
        # to line the three invocations up for readability.
        wanted = line.split()
        assert any(
            wanted == candidate.split()
            for candidate in script.splitlines()
            if candidate.strip().startswith("ledgerloop ")
        ), f"demo.ps1 is missing: {line}"

    assert "ledgerloop.db" in script, "demo.ps1 must clear the database so take two works"
