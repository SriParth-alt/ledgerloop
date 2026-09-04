"""Rule 7, enforced by the build rather than by discipline.

"Never hand-write a metric into README.md" is the project's seventh rule and the only one
with no mechanical guard. Rule 6 has `tests/test_no_truth_leak.py`, which has already
caught a real violation. Rule 7 has had good intentions, and the README's Results table
sat full of placeholder dashes for twelve days while `results/metrics.md` filled up with
real numbers beside it.

So: `evaluate` renders the summary table, writes it to `results/summary.md`, and splices
it into README between markers. These tests assert the two are byte-identical. Editing
either by hand fails the suite — which is the point, because a hand-typed metric is
indistinguishable from a generated one by eye, and that is exactly why the rule exists.

The README keeps its own copy rather than linking out because a judge skimming GitHub
should meet §9.2's centrepiece on the page, not one click away.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eval.ablation import NOT_MEASURED, AblationArm, run_ablation
from eval.summary import RESULTS_END, RESULTS_START, render_summary, splice

REPO_ROOT = Path(__file__).resolve().parent.parent
README = REPO_ROOT / "README.md"
SUMMARY = REPO_ROOT / "results" / "summary.md"


# --- the guard that matters ---------------------------------------------------------


def test_the_readme_results_block_is_byte_identical_to_the_generated_summary() -> None:
    """The whole reason this file exists.

    If someone edits either side by hand — tidying a percentage, rounding a figure,
    'just updating' a number after a run — these diverge and the suite fails. Rule 7
    stops depending on whoever is typing.
    """
    assert SUMMARY.exists(), "run `make eval` — results/summary.md is generated"
    readme = README.read_text(encoding="utf-8")

    start = readme.index(RESULTS_START) + len(RESULTS_START)
    end = readme.index(RESULTS_END)

    assert readme[start:end].strip() == SUMMARY.read_text(encoding="utf-8").strip()


def test_the_readme_no_longer_advertises_itself_as_a_placeholder() -> None:
    """The table shipped as em-dashes with a 'PLACEHOLDER — do not fill by hand' banner.
    Correct while nothing was measured; a false statement about the project once the
    ablation had run."""
    readme = README.read_text(encoding="utf-8")

    assert "PLACEHOLDER" not in readme
    assert "Delete this blockquote once real numbers land" not in readme


# --- rendering ----------------------------------------------------------------------


def _arms(tmp_path: Path) -> dict[str, list[AblationArm]]:
    return {
        "realistic": run_ablation(
            "realistic", records=60, seed=42, workdir=tmp_path, adapter=None, cache=None
        )
    }


def test_every_arm_of_the_ablation_gets_a_row(tmp_path: Path) -> None:
    """§9.2 fixes the progression. A row quietly missing from the summary would read as
    a configuration nobody tried."""
    block = render_summary(_arms(tmp_path))

    for label in ("T0 only", "T0 + T1", "T0 + T1 + T2", "Full cascade", "LLM-only baseline"):
        assert label in block


def test_an_unmeasured_arm_says_so_rather_than_showing_a_blank(tmp_path: Path) -> None:
    """A blank cell reads as an oversight and a zero reads as a measurement that found
    nothing. Only 'not measured' is true — the same argument ADR-032 makes for the
    detailed table, applied to the compact one."""
    block = render_summary(_arms(tmp_path))

    assert NOT_MEASURED in block


def test_the_summary_names_the_fixture_each_number_came_from(tmp_path: Path) -> None:
    """A match rate without its fixture is not reproducible: the same cascade scores
    100% on `easy` and 67.9% on `adversarial`."""
    block = render_summary(_arms(tmp_path))

    assert "realistic" in block


# --- splicing -----------------------------------------------------------------------


def _readme(body: str) -> str:
    return f"# Title\n\nintro\n\n{RESULTS_START}\n{body}\n{RESULTS_END}\n\ntail\n"


def test_splice_replaces_only_what_is_between_the_markers(tmp_path: Path) -> None:
    path = tmp_path / "README.md"
    path.write_text(_readme("old table"), encoding="utf-8")

    splice(path, "new table")
    written = path.read_text(encoding="utf-8")

    assert "new table" in written
    assert "old table" not in written
    assert written.startswith("# Title")
    assert written.rstrip().endswith("tail")


def test_splice_is_idempotent(tmp_path: Path) -> None:
    """`make eval` runs repeatedly. A splice that accumulated copies, or that ate a
    marker on the second pass, would corrupt the README on the day it is read most."""
    path = tmp_path / "README.md"
    path.write_text(_readme("old"), encoding="utf-8")

    splice(path, "table")
    once = path.read_text(encoding="utf-8")
    splice(path, "table")

    assert path.read_text(encoding="utf-8") == once
    assert once.count(RESULTS_START) == 1


def test_splice_refuses_a_readme_with_no_markers(tmp_path: Path) -> None:
    """Silently appending the table, or silently doing nothing, both end with a README
    whose numbers are stale and whose staleness is invisible. Fail loudly instead."""
    path = tmp_path / "README.md"
    path.write_text("# Title\n\nno markers here\n", encoding="utf-8")

    with pytest.raises(ValueError, match="marker"):
        splice(path, "table")


# --- the partial-table footgun ------------------------------------------------------


def _cli() -> object:
    """The Typer app with the eval commands registered.

    `evaluate` is attached to `ledgerloop.cli.app` by importing `eval.cli` — the dependency
    points that way on purpose (ADR-023). Importing it here rather than relying on some
    other test module having done so during collection, which made these tests pass or
    fail depending on collection order.
    """
    import eval.cli  # noqa: F401  - registers the commands as a side effect
    from ledgerloop.cli import app

    return app


def test_a_single_fixture_run_never_splices_the_readme(tmp_path: Path) -> None:
    """`evaluate --fixture easy` must not replace the full table with five rows.

    The README carries every fixture. A single-fixture run knows about one, so splicing
    its result would silently delete two thirds of the published table — and the deletion
    looks exactly like a successful regeneration. Found by running `--fixture easy` while
    chasing a quota reset, which would have quietly gutted the README on the day of
    submission.

    Only `--all-fixtures` writes the published artefacts. Everything else writes its own
    `--out` and stops.
    """


    readme = tmp_path / "README.md"
    original = f"# T\n\n{RESULTS_START}\nORIGINAL TABLE\n{RESULTS_END}\n"
    readme.write_text(original, encoding="utf-8")

    result = CliRunner().invoke(_cli(), [
        "evaluate", "--fixture", "easy", "--records", "60", "--no-llm",
        "--out", str(tmp_path / "m.md"),
        "--summary-out", str(tmp_path / "s.md"),
        "--readme", str(readme),
    ])

    assert result.exit_code == 0, result.output
    assert readme.read_text(encoding="utf-8") == original
    assert "ORIGINAL TABLE" in readme.read_text(encoding="utf-8")


def test_an_all_fixtures_run_does_splice_the_readme(tmp_path: Path) -> None:
    """The other half of the rule: a complete sweep is exactly what the README wants."""


    readme = tmp_path / "README.md"
    readme.write_text(f"# T\n\n{RESULTS_START}\nOLD\n{RESULTS_END}\n", encoding="utf-8")

    result = CliRunner().invoke(_cli(), [
        "evaluate", "--all-fixtures", "--records", "60", "--no-llm",
        "--out", str(tmp_path / "m.md"),
        "--summary-out", str(tmp_path / "s.md"),
        "--readme", str(readme),
    ])

    assert result.exit_code == 0, result.output
    written = readme.read_text(encoding="utf-8")
    assert "OLD" not in written
    assert "adversarial" in written


# --- throughput: the third of the track bar that was measured and never published ----


def test_throughput_is_published_with_its_own_caveats(tmp_path: Path) -> None:
    """Track 04's bar is "throughput plus measured accuracy plus an honest exception
    list", and the guide spells it out: report how many matched, **how fast**, and which
    ones could not be resolved.

    `RunMetrics.throughput` existed from day 8 with a docstring saying "the track bar asks
    for it explicitly" — and no table ever rendered it. Measured, then discarded.

    It lives in its own file rather than the summary because CI proved it cannot live in
    the drift-checked one: the same commit measured 1,340 credits/s locally and 952/s on
    the runner (ADR-041).
    """
    arms = {
        "realistic": run_ablation(
            "realistic", records=60, seed=42, workdir=tmp_path, adapter=None, cache=None
        )
    }
    from eval.summary import render_throughput

    block = render_throughput(arms)

    assert "Credits/s" in block
    # And it must disclaim itself: a timing is not reproducible across machines, and
    # every other published figure is. Publishing it without that line would quietly
    # weaken the guarantee that makes the others worth trusting.
    assert "Not byte-reproducible" in block
    assert "excludes API latency" in block


def test_throughput_is_reported_per_second_not_as_a_duration(tmp_path: Path) -> None:
    """A duration depends on batch size; a rate does not. "45 seconds" says nothing
    without the record count beside it, and the count is what a reader forgets first."""
    arms = {
        "adversarial": run_ablation(
            "adversarial", records=60, seed=42, workdir=tmp_path, adapter=None, cache=None
        )
    }
    scored = next(a for a in arms["adversarial"] if a.metrics is not None)

    assert scored.metrics is not None
    assert scored.metrics.throughput > 0
    assert scored.metrics.seconds > 0


def test_the_demo_target_can_be_run_twice(tmp_path: Path) -> None:
    """`make demo` is the command the pitch video runs, and it has to survive take two.

    Matches are append-only and a run id is unique (ADR-013), so reconciling into an
    existing run id is refused — correctly. That made the demo target single-use: rehearse
    once, then watch it error on camera. The target clears its own database first.
    """
    makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
    demo = makefile.split("demo:", 1)[1].split("\n\n", 1)[0]

    assert "ledgerloop.db" in demo, "make demo does not clear its database — take two fails"


def test_a_single_fixture_run_never_overwrites_the_published_metrics(
    tmp_path: Path,
) -> None:
    """The same footgun ADR-039 closed, through the other door.

    That ADR stopped a single-fixture run publishing to `results/summary.md` and the README.
    It left `--out`, which still defaulted to `results/metrics.md` — so
    `evaluate --fixture easy` would replace the full three-fixture table with an easy-only
    one, and the replacement looks exactly like a successful regeneration.

    Found while probing whether a quota had reset: the third time this class of bug has
    surfaced during a routine operation rather than in a test.

    Invoked with the real default so the guard is exercised against the path it protects.
    Safe, because the refusal happens before any work — which is also the point: a run that
    is going to refuse to publish should refuse before it spends a quota.
    """
    published = REPO_ROOT / "results" / "metrics.md"
    before = published.read_bytes() if published.exists() else None

    result = CliRunner().invoke(_cli(), [
        "evaluate", "--fixture", "easy", "--records", "60", "--no-llm",
        "--summary-out", str(tmp_path / "s.md"),
        "--readme", str(tmp_path / "nonexistent.md"),
    ])

    assert result.exit_code != 0, result.output
    assert "all-fixtures" in result.output
    assert (published.read_bytes() if published.exists() else None) == before


def test_a_single_fixture_run_writes_where_it_is_told_to(tmp_path: Path) -> None:
    """The refusal must be about protecting the *published* path, not about making partial
    runs useless. Point it somewhere else and it writes there."""
    out = tmp_path / "scratch" / "easy.md"

    result = CliRunner().invoke(_cli(), [
        "evaluate", "--fixture", "easy", "--records", "60", "--no-llm",
        "--out", str(out),
        "--summary-out", str(tmp_path / "s.md"),
        "--readme", str(tmp_path / "nonexistent.md"),
    ])

    assert result.exit_code == 0, result.output
    assert out.exists()
    assert "easy" in out.read_text(encoding="utf-8")
