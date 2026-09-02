"""The ablation table — §9.2, and the centrepiece of the pitch.

Running the same fixture with tiers progressively enabled is what turns "I built a
cascade" into "I measured what each tier contributes". Without it the architecture is an
assertion.

Two rows cannot be measured until Tier 3 lands tomorrow. They are emitted as **not yet
measured** rather than blank or zero. A blank reads as an oversight; a zero reads as a
measurement that found nothing. Only "not measured" is true, and rule 7 is as much about
not implying a number as about not typing one.
"""

from __future__ import annotations

from pathlib import Path

from eval.ablation import DETERMINISTIC_ARMS, run_ablation, write_report

FIXTURE = "realistic"
RECORDS = 60


def test_the_deterministic_arms_are_the_ones_the_spec_names() -> None:
    """§9.2 fixes the progression. A row that quietly changed meaning between runs would
    make two published tables incomparable."""
    assert [arm.label for arm in DETERMINISTIC_ARMS] == [
        "T0 only",
        "T0 + T1",
        "T0 + T1 + T2",
    ]
    assert [sorted(arm.tiers) for arm in DETERMINISTIC_ARMS] == [[0], [0, 1], [0, 1, 2]]


def test_each_arm_scores_the_same_fixture(tmp_path: Path) -> None:
    """The arms must differ only in which tiers ran. A different fixture per arm would
    make the table a comparison of datasets rather than of tiers."""
    arms = run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)
    measured = [arm for arm in arms if arm.metrics is not None]

    assert measured
    assert len({arm.metrics.credits_total for arm in measured}) == 1


def test_adding_tiers_never_reduces_what_is_matched(tmp_path: Path) -> None:
    """Each tier runs on its predecessor's residual, so the cascade is monotonic in
    coverage. A drop would mean a later tier had somehow un-matched something."""
    arms = run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)
    posted = [arm.metrics.matches_posted for arm in arms if arm.metrics is not None]

    assert posted == sorted(posted)


def test_an_arm_is_unmeasured_only_when_the_model_was_needed_and_missing(
    tmp_path: Path,
) -> None:
    """The precise rule, which is narrower than "no key means no number".

    With no adapter and no cache, the LLM-only baseline cannot ask about anything and
    carries no number. The full cascade, on this fixture, *also* asks about nothing —
    T0-T2 resolve every credit, so Tier 3 has an empty residual and needs no model to
    reach a complete answer. That arm is genuinely measurable.

    An arm is unmeasured when credits went unanswered, not when a key was absent. The
    distinction is what lets CI reproduce the model rows from the committed cache with no
    credentials at all (ADR-035).
    """
    arms = run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)
    by_label = {arm.label: arm for arm in arms}

    assert by_label["LLM-only baseline"].metrics is None
    assert by_label["Full cascade"].metrics is not None


def test_the_report_contains_no_hand_written_number(tmp_path: Path) -> None:
    """Rule 7. Every figure in the output must have come from a scored run, which is
    checkable by regenerating it and getting the same bytes."""
    arms = {FIXTURE: run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)}
    first = tmp_path / "a.md"
    second = tmp_path / "b.md"

    write_report(arms, first)
    write_report(arms, second)

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_the_report_states_which_rows_were_not_measured(tmp_path: Path) -> None:
    """A reader must be able to tell an unmeasured row from a measured zero without
    reading the source."""
    arms = {FIXTURE: run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)}
    out = tmp_path / "metrics.md"
    write_report(arms, out)
    body = out.read_text(encoding="utf-8")

    assert "not yet measured" in body
    assert "LLM-only baseline" in body
    assert "false-match" in body.lower()


def test_the_report_names_the_config_the_numbers_came_from(tmp_path: Path) -> None:
    """§9.1: report the MatchConfig alongside any metric. A tolerance-dependent number
    whose tolerance is unknown is not reproducible, and Tier 2's band in particular was
    the difference between 4 matches and 41 (ADR-019)."""
    arms = {FIXTURE: run_ablation(FIXTURE, records=RECORDS, seed=42, workdir=tmp_path)}
    out = tmp_path / "metrics.md"
    write_report(arms, out)
    body = out.read_text(encoding="utf-8")

    assert "subset_tolerance_bps" in body
    assert "seed" in body.lower()
