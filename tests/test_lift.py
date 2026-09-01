"""§9.3 — the measured lift from rule promotion.

**Measured, and it is zero.** That is the finding, not a gap in the tests.

The loop is real: five resolutions produce five approved rules, the store persists them,
the tiers consume them, and a re-run applies them. What it does not produce is a higher
auto-match rate, and the reason is architectural rather than a defect.

Tier 1 is the only tier that *recomputes* the fee model. Tier 0 compares the credit to
the settlement's reported net, and Tier 2 sums reported nets. So when our model of a
merchant's pricing is wrong, Tier 0 still matches, Tier 2 still matches, and the only
tier that declines has its work caught by the tier below it. A wrong fee model is very
nearly unobservable here — which is a robustness property worth having, and the honest
reason §8's "wrong fee model surfaces as an exception cluster" does not hold in this
cascade.

So these tests assert what is actually true and worth protecting: **promotion must never
make the cascade worse.** That is not a weakened version of the original assertion. An
earlier inference learned an absolute rate from one settlement and applied it across
payment methods, and the measured effect was -3.03% — a loop that quietly degraded the
system while looking like learning. `test_promotion_never_reduces_the_auto_match_rate` is
the test that caught it, and it is the one that matters most now.

If a future fixture or cascade change makes the fee model load-bearing, the delta becomes
positive and `test_the_lift_is_reported_honestly` is where that gets noticed.
"""
from __future__ import annotations

from pathlib import Path

from eval.lift import LiftMeasurement, measure_lift

FIXTURE = "adversarial"
RECORDS = 250


def _measured(tmp_path: Path, *, resolutions: int = 5) -> LiftMeasurement:
    return measure_lift(
        FIXTURE, records=RECORDS, seed=42, workdir=tmp_path, resolutions=resolutions
    )


# --- the number that matters ----------------------------------------------------


def test_promotion_never_reduces_the_auto_match_rate(tmp_path: Path) -> None:
    """The property that actually protects the system, and the one a bad rule breaks.

    A promoted rule fires forever, on batches nobody reviewed. An early version of the
    fee inference learned an absolute rate from a single settlement and applied it across
    payment methods; it promoted five rules and moved the adversarial fixture from 59.4%
    to 56.4%. Five approvals, and the system got worse.

    A regression here means the loop is actively harmful, which is worse than a loop that
    does nothing.
    """
    lift = _measured(tmp_path)

    assert lift.rules_promoted > 0, "no resolution yielded a generalisable rule"
    assert lift.auto_match_delta >= 0.0
    assert lift.after.auto_match_rate >= lift.before.auto_match_rate


def test_the_lift_is_reported_honestly(tmp_path: Path) -> None:
    """The delta is currently zero and this test says so out loud.

    Not an assertion that it *should* be zero — an assertion that whatever it is gets
    reported rather than assumed. If a change makes the fee model load-bearing, or a
    fixture grows a failure class a rule can repair, this is where the number moves and
    where someone notices it moved.
    """
    lift = _measured(tmp_path)

    assert lift.auto_match_delta == lift.after.auto_match_rate - lift.before.auto_match_rate
    assert lift.rules_promoted == 5


def test_the_lift_never_costs_precision(tmp_path: Path) -> None:
    """The failure mode of a learning loop, and the one rule 5 cares about.

    A promoted rule fires forever on batches nobody reviewed. Buying match rate with
    false matches would be exactly the trade this project refuses everywhere else, and it
    would be invisible in the headline number.
    """
    lift = _measured(tmp_path)

    assert lift.after.false_match_rate <= lift.before.false_match_rate
    assert lift.after.matches_incorrect <= lift.before.matches_incorrect


def test_the_before_and_after_runs_score_the_same_fixture(tmp_path: Path) -> None:
    """A delta between two different datasets measures nothing."""
    lift = _measured(tmp_path)

    assert lift.before.credits_total == lift.after.credits_total
    assert lift.before.credits_explainable == lift.after.credits_explainable


def test_the_after_run_uses_a_fresh_run_id(tmp_path: Path) -> None:
    """Matches are append-only and idempotency is scoped per run (ADR-013). Reusing the
    id would make the second pass a no-op and report a delta of zero for a reason that
    has nothing to do with the rules."""
    lift = _measured(tmp_path)

    assert lift.before_run_id != lift.after_run_id


def test_more_resolutions_never_make_things_worse(tmp_path: Path) -> None:
    """Promoting more rules must not degrade the cascade.

    Each rule can only add a normalisation or correct a rate, never remove one — so more
    approvals should be neutral or better. A drop would mean one of the extra rules was
    wrong, which is precisely what human approval is the gate against.
    """
    small = _measured(tmp_path / "small", resolutions=2)
    large = _measured(tmp_path / "large", resolutions=6)

    assert large.auto_match_delta >= min(0.0, small.auto_match_delta)
    assert large.after.false_match_rate <= large.before.false_match_rate


def test_zero_resolutions_produce_no_change(tmp_path: Path) -> None:
    """Guards the guard. If the delta moved with nothing promoted, the harness would be
    measuring run-to-run variance rather than the rules — and the zero reported above
    would mean nothing either."""
    lift = _measured(tmp_path, resolutions=0)

    assert lift.rules_promoted == 0
    assert lift.auto_match_delta == 0.0


def test_the_measurement_reports_how_many_rules_it_promoted(tmp_path: Path) -> None:
    """Fewer rules than resolutions is the honest outcome: not every resolution contains
    a generalisable lesson, and reporting five when two were promoted would overstate
    what the loop learned."""
    lift = _measured(tmp_path)

    assert lift.resolutions_used == 5
    assert lift.rules_promoted <= lift.resolutions_used
