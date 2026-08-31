"""Tier 2 — subset-sum for batched payouts.

One bank credit covers N settlements and nothing in the statement says which N. This
tier works out the subset, and — more importantly — refuses to when more than one
subset fits.

**The question is not "which subsets sum to C". It is "is there exactly one".** That
distinction is the whole tier. Enumerating every subset of a 25-element pool is 33
million possibilities; deciding between zero, one and two-or-more needs at most two
solutions found, and the search can stop the moment it has them.

Read `find_subsets` as a decision procedure with three outcomes:

    zero solutions   -> fall through to Tier 3
    exactly one      -> post at 0.95, evidence naming every member
    two or more      -> AMBIGUOUS_SUBSET carrying all explanations, and the credit is
                        terminal: it does not reach Tier 3, because §7.5 forbids the
                        model from resolving ambiguity

Note that Tier 2 sums the settlements' **reported** `net_amount_paise` rather than
recomputing each from the fee model as Tier 1 does. A batch's credit is the sum of what
actually settled, including any refund that netted against the cycle — which the fee
model cannot predict.
"""

from __future__ import annotations

from datetime import date

from ledgerloop.audit.provenance import ProposedException
from ledgerloop.cascade.tier2_subsetsum import (
    CONFIDENCE,
    RULE_SUBSET_SUM,
    candidate_pool,
    find_subsets,
    match_tier2,
)
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, PaymentMethod
from ledgerloop.ingest.schemas import BankRow, SettlementRow

DAY = date(2026, 8, 10)
BUDGET = DEFAULT_MATCH_CONFIG.subset_search_node_budget
MAX_MEMBERS = DEFAULT_MATCH_CONFIG.subset_max_members


def bank(txn_id: str = "BNK1", *, credit: int, value_date: date = DAY) -> BankRow:
    return BankRow(
        bank_txn_id=txn_id,
        value_date=value_date,
        narration="NEFT-CR/HDFC/BATCH PAYOUT/BLR",
        credit_paise=credit,
        debit_paise=0,
        balance_paise=credit,
    )


def settlement(
    settlement_id: str, *, net: int, settled_on: date = DAY
) -> SettlementRow:
    """A settlement with fees already zeroed, so `net` is exactly what it says.

    Tier 2 reasons about reported nets, so controlling them directly keeps these tests
    about the search rather than about the fee model.
    """
    return SettlementRow(
        settlement_id=settlement_id,
        payment_id=f"PAY{settlement_id}",
        order_id=f"ORD{settlement_id}",
        invoice_ref=f"INV{settlement_id}",
        customer_name="ACME RETAIL PVT LTD",
        method=PaymentMethod.UPI,
        gross_amount_paise=net,
        fee_paise=0,
        gst_on_fee_paise=0,
        tds_paise=0,
        net_amount_paise=net,
        captured_at=DAY,
        settled_on=settled_on,
        utr=f"RZRPY{settlement_id[-7:].rjust(7, '0')}",
        status="captured",
    )


def search(pool, target, *, tolerance=0, max_members=MAX_MEMBERS, budget=BUDGET, stop_after=2):
    return find_subsets(
        pool,
        target,
        tolerance_paise=tolerance,
        max_members=max_members,
        node_budget=budget,
        stop_after=stop_after,
    )


# =================================================================================
# find_subsets — the decision procedure
# =================================================================================


def test_finds_a_single_member_subset() -> None:
    pool = [settlement("STL1", net=500), settlement("STL2", net=700)]

    result = search(pool, 500)

    assert result.solutions == (("STL1",),)
    assert result.exhausted is True


def test_finds_a_two_member_batch() -> None:
    """The case the tier exists for: a credit covering two payments, with nothing in
    the statement saying which two."""
    pool = [settlement("STL1", net=300), settlement("STL2", net=200), settlement("STL3", net=999)]

    result = search(pool, 500)

    assert result.solutions == (("STL1", "STL2"),)


def test_finds_nothing_when_no_subset_reaches_the_target() -> None:
    """Zero solutions is a real answer, not a failure. The credit falls through to
    Tier 3 with the pool untouched."""
    pool = [settlement("STL1", net=300), settlement("STL2", net=200)]

    result = search(pool, 1_000)

    assert result.solutions == ()
    assert result.exhausted is True


def test_reports_two_solutions_when_two_exist() -> None:
    """The decoy case. Both subsets are arithmetically perfect; there is no principled
    tiebreak, and the tier's job is to notice that rather than to choose."""
    pool = [
        settlement("STL1", net=300),
        settlement("STL2", net=200),
        settlement("STL3", net=500),
    ]

    result = search(pool, 500)

    assert len(result.solutions) == 2
    assert set(result.solutions) == {("STL1", "STL2"), ("STL3",)}


def test_search_stops_once_it_has_enough_solutions() -> None:
    """`stop_after` is what makes this tractable. Deciding between zero, one and
    two-or-more never needs a third solution, and enumerating a 25-element pool would
    be 33 million subsets."""
    pool = [settlement(f"STL{i}", net=100) for i in range(1, 9)]

    result = search(pool, 200, stop_after=2)

    assert len(result.solutions) == 2


def test_solutions_are_deterministic() -> None:
    """Two runs over the same pool must agree, or a re-run of the same batch produces a
    different set of matches and reconciliation stops being reproducible for audit."""
    pool = [settlement("STL1", net=300), settlement("STL2", net=200), settlement("STL3", net=500)]

    assert search(pool, 500).solutions == search(pool, 500).solutions


def test_each_member_is_used_at_most_once() -> None:
    """A settlement cannot pay for itself twice. Without this the search would 'solve'
    almost any target by reusing one row."""
    pool = [settlement("STL1", net=250)]

    result = search(pool, 500)

    assert result.solutions == ()


def test_tolerance_admits_paise_drift() -> None:
    """PAISE_DRIFT moves a credit by one to three paise. A batch that is three paise
    short is still that batch."""
    pool = [settlement("STL1", net=300), settlement("STL2", net=200)]

    assert search(pool, 497, tolerance=3).solutions == (("STL1", "STL2"),)
    assert search(pool, 497, tolerance=0).solutions == ()


def test_subset_size_is_capped() -> None:
    """A credit covering more members than the cap is not something we auto-post.
    Real batches are two to five; the cap is policy, and it prunes hard."""
    pool = [settlement(f"STL{i}", net=100) for i in range(1, 7)]

    assert search(pool, 400, max_members=4).solutions
    assert search(pool, 400, max_members=3).solutions == ()


def test_an_exhausted_node_budget_is_reported_rather_than_hidden() -> None:
    """The pool cap alone does not bound the work: 2^25 is 33 million nodes. The budget
    is the real guard, and a search that ran out must say so — a truncated search that
    found one solution has not shown that solution is unique.
    """
    # Every amount is even and the target is odd, so no subset can ever hit it exactly.
    # The prunes cannot short-circuit that — the target sits well inside the reachable
    # range — so the search is forced to explore, which is what the budget is for.
    pool = [settlement(f"STL{i:02d}", net=2 * i) for i in range(1, 21)]

    result = search(pool, 211, budget=25)

    assert result.exhausted is False
    assert result.solutions == (), "a search that gave up must not report findings"


def test_an_empty_pool_yields_nothing() -> None:
    assert search([], 500).solutions == ()


# =================================================================================
# candidate_pool — what the search is allowed to see
# =================================================================================


def test_pool_includes_settlements_whose_window_covers_the_credit() -> None:
    inside = settlement("STL1", net=100, settled_on=DAY)
    result = candidate_pool(
        bank(credit=100, value_date=DAY),
        [inside],
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )

    assert [row.settlement_id for row in result] == ["STL1"]


def test_pool_excludes_settlements_that_had_not_settled_yet() -> None:
    """Money cannot arrive before the gateway released it."""
    later = settlement("STL1", net=100, settled_on=date(2026, 8, 20))
    result = candidate_pool(
        bank(credit=100, value_date=DAY),
        [later],
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )

    assert result == []


def test_pool_excludes_settlements_beyond_the_window() -> None:
    stale = settlement("STL1", net=100, settled_on=date(2026, 7, 1))
    result = candidate_pool(
        bank(credit=100, value_date=DAY),
        [stale],
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )

    assert result == []


# =================================================================================
# match_tier2 — the three outcomes
# =================================================================================


def _result(bank_txns, settlements):
    return match_tier2(
        bank_txns, settlements, fee_model=SETTLEMENT_FEE_MODEL, config=DEFAULT_MATCH_CONFIG
    )


def test_a_unique_batch_posts_naming_every_member() -> None:
    """§6: evidence lists every member. A record naming only the first would understate
    what the credit actually covers, and the other members would look unexplained."""
    outcome = _result(
        [bank(credit=500)],
        [settlement("STL1", net=300), settlement("STL2", net=200), settlement("STL3", net=999)],
    )

    assert len(outcome.matches) == 1
    match = outcome.matches[0]
    assert match.tier == 2
    assert match.rule_id == RULE_SUBSET_SUM
    assert match.confidence == CONFIDENCE
    assert set(match.settlement_ids) == {"STL1", "STL2"}
    assert {item.settlement_value for item in match.evidence} >= {"STL1", "STL2"}
    assert outcome.exceptions == []


def test_two_valid_explanations_raise_ambiguous_subset_and_match_nothing() -> None:
    """ADR-003, and the centrepiece of the demo. A naive matcher picks one and is wrong
    half the time. Both explanations are attached so a human can choose in two clicks.
    """
    outcome = _result(
        [bank(credit=500)],
        [
            settlement("STL1", net=300),
            settlement("STL2", net=200),
            settlement("STL3", net=500),
        ],
    )

    assert outcome.matches == []
    assert len(outcome.exceptions) == 1

    raised: ProposedException = outcome.exceptions[0]
    assert raised.code is ExceptionCode.AMBIGUOUS_SUBSET
    assert raised.bank_txn_id == "BNK1"
    assert raised.value_at_risk_paise == 500

    explanations = raised.detail["candidate_subsets"]
    assert len(explanations) == 2
    assert {tuple(subset) for subset in explanations} == {("STL1", "STL2"), ("STL3",)}


def test_no_solution_falls_through_without_an_exception() -> None:
    """Zero solutions is Tier 3's cue, not a problem to report. Raising here would fill
    the queue with rows the next tier was about to resolve."""
    outcome = _result([bank(credit=9_999)], [settlement("STL1", net=100)])

    assert outcome.matches == []
    assert outcome.exceptions == []


def test_a_pool_over_the_cap_declines_rather_than_searching() -> None:
    """§6's complexity guard. Pruning to the cap and searching anyway would be worse
    than declining: the true batch might include a settlement that pruning discarded,
    so 'exactly one solution' would be a claim about a pool we already truncated.
    Bounded compute is only a signal if the bound is real.
    """
    oversized = [
        settlement(f"STL{i}", net=100)
        for i in range(1, DEFAULT_MATCH_CONFIG.subset_pool_cap + 5)
    ]

    outcome = _result([bank(credit=200)], oversized)

    assert outcome.matches == []
    assert len(outcome.exceptions) == 1
    assert outcome.exceptions[0].code is ExceptionCode.POOL_TOO_LARGE


def test_an_exhausted_budget_declines_rather_than_posting() -> None:
    """A search that ran out of budget has not proved uniqueness. Posting its first
    solution would assert something the search never established."""
    pool = [settlement(f"STL{i:02d}", net=2_000 * i) for i in range(1, 21)]
    starved = DEFAULT_MATCH_CONFIG.__class__(subset_search_node_budget=5)

    outcome = match_tier2(
        [bank(credit=211_000)], pool, fee_model=SETTLEMENT_FEE_MODEL, config=starved
    )

    assert outcome.matches == []
    assert len(outcome.exceptions) == 1
    assert outcome.exceptions[0].code is ExceptionCode.POOL_TOO_LARGE


def test_a_settlement_is_never_claimed_by_two_credits() -> None:
    """Two credits cannot both be explained by the same batch. Posting both would
    double-count the money and no later tier recomputes it."""
    outcome = _result(
        [bank("BNK1", credit=500), bank("BNK2", credit=500)],
        [settlement("STL1", net=300), settlement("STL2", net=200)],
    )
    claimed = [sid for match in outcome.matches for sid in match.settlement_ids]

    assert len(claimed) == len(set(claimed))


def test_a_one_member_subset_is_a_valid_batch() -> None:
    """A subset of size one is still a subset.

    A 1:1 credit reaching Tier 2 was declined by Tier 0 and Tier 1 for *their* reasons —
    a non-unique amount key, no recoverable reference. Subset arithmetic is an
    independent basis, so finding exactly one subset that sums to the credit is real
    evidence and posts at 0.95 rather than the certainty Tier 0 would have claimed.
    """
    outcome = _result([bank(credit=100)], [settlement("STL1", net=100)])

    assert [match.rule_id for match in outcome.matches] == [RULE_SUBSET_SUM]
    assert outcome.matches[0].settlement_ids == ("STL1",)
