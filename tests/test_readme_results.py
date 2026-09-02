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
