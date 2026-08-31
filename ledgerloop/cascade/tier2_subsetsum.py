"""Tier 2 — subset-sum for batched payouts. The technically interesting tier.

For each unmatched bank credit, build a candidate pool (settlements inside the date
window), then find whether any subset of it sums to the credit within tolerance.

**The question is not "which subsets sum to C". It is "is there exactly one".** That
reframing is what makes the tier tractable: enumerating every subset of a 25-element
pool is 33 million possibilities, but deciding between zero, one and two-or-more never
needs a third solution. The search stops the moment it has two.

    zero solutions   -> fall through to Tier 3
    exactly one      -> post at 0.95, evidence naming every member
    two or more      -> AMBIGUOUS_SUBSET carrying every explanation, and the credit is
                        terminal — it does not reach Tier 3, because §7.5 forbids the
                        model from resolving ambiguity
    pool over cap    -> POOL_TOO_LARGE, declined without searching

Ambiguous solutions are never ranked. Both are arithmetically perfect, there is no
principled tiebreak, and picking is a coin flip on the books.

**Two bounds, and only one of them is real.** §6 caps the pool at 25 by nearest-date
pruning. A cap alone does not bound the work — 25 members still permits 2^25 nodes — so
the search also carries an explicit node budget, and a search that exhausts it is
discarded rather than posted. A truncated search that found one solution has not shown
that solution is unique.

**Why an oversized pool is declined rather than pruned.** §6 says to prune to the cap
and then says to decline if the pool exceeds it, which cannot both apply. Declining is
the safer reading: pruning to the nearest 25 and searching anyway would let "exactly one
solution" become a claim about a pool we had already truncated, and the true batch might
include a settlement the pruning discarded. A confidently wrong unique answer is exactly
what rule 5 exists to prevent.

Tier 2 sums the settlements' **reported** ``net_amount_paise`` rather than recomputing
each from the fee model as Tier 1 does. A batch's credit is the sum of what actually
settled, including a refund that netted against the cycle, which the fee model cannot
predict.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from ledgerloop.audit.provenance import (
    MatchEvidence,
    ProposedException,
    ProposedMatch,
    TierResult,
)
from ledgerloop.config import DEFAULT_MATCH_CONFIG, MatchConfig
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, FeeModel
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.money import apply_rate

RULE_SUBSET_SUM = "T2-SUBSET-SUM"

TIER = 2

#: §6 fixes this. Below Tier 0's certainty and below a corroborated Tier 1 match: a
#: subset that adds up is strong evidence, but the tier has inferred the grouping
#: rather than read it from anything the bank told us.
CONFIDENCE = 0.95


@dataclass(frozen=True)
class SubsetSearch:
    """The outcome of one subset search.

    ``exhausted`` is False when the node budget stopped the search early. That
    distinction matters: an unexhausted search that found one solution has *not*
    established that the solution is unique, so it must not be posted.
    """

    solutions: tuple[tuple[str, ...], ...]
    exhausted: bool


def tolerance_for(credit_paise: int, config: MatchConfig) -> int:
    """The band a subset sum may miss the credit by.

    Flat by default, unlike Tier 1's. Tier 1 recomputes the expected net from the fee
    model and needs a relative component to absorb that imprecision; Tier 2 sums reported
    nets, so the only error to absorb is PAISE_DRIFT of one to three paise.

    A relative band here would not absorb error — it would manufacture ambiguity, because
    a wide window over a twenty-five settlement pool admits many subsets that are not
    batches. ``subset_tolerance_bps`` exists so the ablation can vary this rather than
    take it on faith (ADR-019).
    """
    return max(
        config.amount_tolerance_paise,
        apply_rate(credit_paise, config.subset_tolerance_bps),
    )


def candidate_pool(
    bank_txn: BankRow,
    settlements: Sequence[SettlementRow],
    *,
    fee_model: FeeModel,
    config: MatchConfig,
) -> list[SettlementRow]:
    """Settlements that could plausibly be part of this credit.

    §6 also specifies filtering by ``merchant_id``. Settlements do not carry one — only
    invoices do — so that filter is unimplementable as written and is omitted. The
    fixtures are single-merchant, so it changes nothing today, but a multi-merchant
    batch would need the join through ``invoice_ref``.

    Settlements larger than the credit are dropped. With every amount positive, a
    settlement whose net exceeds the credit cannot be a member of any subset summing to
    it — that is a proof, not a heuristic, so the filter costs no solutions. It matters
    a great deal in practice: a three-business-day window over a dense batch yields
    pools around fifty, and without this filter almost every credit would be declined
    as POOL_TOO_LARGE rather than solved.

    Sorted by identifier so the pool, and therefore the search, is deterministic.
    """
    ceiling = bank_txn.credit_paise + tolerance_for(bank_txn.credit_paise, config)
    pool = [
        row
        for row in settlements
        if row.net_amount_paise <= ceiling
        and row.settled_on
        <= bank_txn.value_date
        <= fee_model.business_days_after(row.settled_on, config.settlement_slack_days)
    ]
    return sorted(pool, key=lambda row: row.settlement_id)


def find_subsets(
    pool: Sequence[SettlementRow],
    target_paise: int,
    *,
    tolerance_paise: int,
    max_members: int,
    node_budget: int,
    stop_after: int = 2,
) -> SubsetSearch:
    """Find up to ``stop_after`` distinct subsets of ``pool`` summing to the target.

    Depth-first include/exclude over the pool sorted **descending** by net amount, with
    three prunes and a node budget.

    Descending order is not cosmetic. Large amounts first make the running sum cross the
    target early, so the overshoot prune fires near the top of the tree where it cuts the
    biggest subtrees.

    The prunes, all of which rely on every amount being positive:

    * **overshoot** — adding this member already exceeds the band, and every later
      addition only makes it worse;
    * **unreachable** — even taking every remaining member we fall short of the band;
    * **size cap** — a batch larger than ``max_members`` is policy-excluded rather than
      searched.

    A solution is recorded at the moment a member is *added*, never on entry to a node.
    Checking on entry records the same subset once per level as the recursion walks down
    excluding later members, which would make a unique batch look ambiguous. Recording on
    inclusion also means the empty subset can never be a solution.

    Worst case is O(2^N), which is why ``node_budget`` exists. Exhausting it returns
    ``exhausted=False``, and the caller must discard the result: a search that stopped
    early may have found one solution without ever establishing that it is the only one.
    """
    ordered = sorted(pool, key=lambda row: (-row.net_amount_paise, row.settlement_id))
    amounts = [row.net_amount_paise for row in ordered]
    identifiers = [row.settlement_id for row in ordered]

    # suffix[i] is the sum of everything from i onward, so the "unreachable" prune is
    # a single comparison rather than a scan.
    suffix = [0] * (len(amounts) + 1)
    for index in range(len(amounts) - 1, -1, -1):
        suffix[index] = suffix[index + 1] + amounts[index]

    lower = target_paise - tolerance_paise
    upper = target_paise + tolerance_paise

    solutions: list[tuple[str, ...]] = []
    nodes = 0
    exhausted = True

    def walk(index: int, total: int, chosen: list[int]) -> None:
        nonlocal nodes, exhausted

        if len(solutions) >= stop_after or not exhausted:
            return

        nodes += 1
        if nodes > node_budget:
            exhausted = False
            return

        if index >= len(amounts):
            return
        if total + suffix[index] < lower:
            return

        amount = amounts[index]
        if len(chosen) < max_members and total + amount <= upper:
            chosen.append(index)
            included = total + amount
            if included >= lower:
                solutions.append(tuple(sorted(identifiers[i] for i in chosen)))
            walk(index + 1, included, chosen)
            chosen.pop()

        walk(index + 1, total, chosen)

    walk(0, 0, [])

    if not exhausted:
        return SubsetSearch(solutions=(), exhausted=False)
    return SubsetSearch(solutions=tuple(sorted(solutions)), exhausted=True)


def match_tier2(
    bank_txns: Sequence[BankRow],
    settlements: Sequence[SettlementRow],
    *,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
) -> TierResult:
    """Resolve batched credits, and decline the ones that more than one batch explains."""
    claimed: set[str] = set()
    matches: list[ProposedMatch] = []
    exceptions: list[ProposedException] = []

    for bank_txn in bank_txns:
        available = [row for row in settlements if row.settlement_id not in claimed]
        pool = candidate_pool(bank_txn, available, fee_model=fee_model, config=config)
        if not pool:
            continue

        if len(pool) > config.subset_pool_cap:
            exceptions.append(
                _declined(
                    bank_txn,
                    reason=f"candidate pool of {len(pool)} exceeds the cap of "
                    f"{config.subset_pool_cap}; searching a truncated pool could not "
                    "establish that a solution is unique",
                    pool_size=len(pool),
                )
            )
            continue

        search = find_subsets(
            pool,
            bank_txn.credit_paise,
            tolerance_paise=tolerance_for(bank_txn.credit_paise, config),
            max_members=config.subset_max_members,
            node_budget=config.subset_search_node_budget,
        )

        if not search.exhausted:
            exceptions.append(
                _declined(
                    bank_txn,
                    reason="subset search exhausted its node budget; uniqueness was "
                    "never established, so any solution found is not safe to post",
                    pool_size=len(pool),
                )
            )
            continue

        if not search.solutions:
            continue  # Tier 3's problem, not an exception.

        if len(search.solutions) > 1:
            exceptions.append(_ambiguous(bank_txn, search.solutions))
            continue

        members = search.solutions[0]
        matches.append(_posted(bank_txn, members, pool))
        claimed.update(members)

    return TierResult(matches=matches, exceptions=exceptions)


def _posted(
    bank_txn: BankRow, members: tuple[str, ...], pool: Sequence[SettlementRow]
) -> ProposedMatch:
    """Build the match, naming every member in the evidence.

    §6 requires all of them. A record citing only the first would understate what the
    credit covers, and the remaining members would look unexplained in the audit trail.
    """
    by_id = {row.settlement_id: row for row in pool}
    evidence = [
        MatchEvidence(
            field="net_amount_paise",
            bank_value=str(bank_txn.credit_paise),
            settlement_value=member,
            note=f"batch member contributing {by_id[member].net_amount_paise} paise",
        )
        for member in members
    ]
    evidence.append(
        MatchEvidence(
            field="subset_sum",
            bank_value=str(bank_txn.credit_paise),
            settlement_value=str(sum(by_id[member].net_amount_paise for member in members)),
            note=f"exactly one subset of {len(pool)} candidates explains this credit",
        )
    )
    return ProposedMatch(
        bank_txn_id=bank_txn.bank_txn_id,
        settlement_ids=members,
        tier=TIER,
        rule_id=RULE_SUBSET_SUM,
        confidence=CONFIDENCE,
        evidence=tuple(evidence),
    )


def _ambiguous(
    bank_txn: BankRow, solutions: tuple[tuple[str, ...], ...]
) -> ProposedException:
    """Every explanation is attached, because the choice is presented, not made."""
    return ProposedException(
        code=ExceptionCode.AMBIGUOUS_SUBSET,
        bank_txn_id=bank_txn.bank_txn_id,
        settlement_id=None,
        value_at_risk_paise=bank_txn.credit_paise,
        detail={
            "candidate_subsets": [list(subset) for subset in solutions],
            "note": (
                "Two or more subsets explain this credit exactly. Both are "
                "arithmetically perfect, so there is no principled tiebreak and the "
                "choice belongs to a human."
            ),
        },
    )


def _declined(bank_txn: BankRow, *, reason: str, pool_size: int) -> ProposedException:
    return ProposedException(
        code=ExceptionCode.POOL_TOO_LARGE,
        bank_txn_id=bank_txn.bank_txn_id,
        settlement_id=None,
        value_at_risk_paise=bank_txn.credit_paise,
        detail={"pool_size": pool_size, "note": reason},
    )
