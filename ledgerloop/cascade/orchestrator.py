"""Runs the tiers in order and records the outcome of every record.

Executes T0 -> T1 -> T2 -> T3 -> T4, emitting a per-tier count as it goes (this
streaming output is the demo).

Supports ``--tiers 0,1,2`` so the ablation harness can run partial configurations
against the same fixture, and ``--no-llm`` producing a ``degraded=true`` run that still
completes: if the model is unavailable the batch finishes without Tier 3, auto-match
rate falls, correctness does not.

Two things are worth being precise about.

**Ablation is not degradation.** ``--tiers 0,1,2`` is a deliberate configuration and its
numbers are directly comparable to other arms of the same table. ``--no-llm`` while tier
3 was asked for is a failure the run survived, and its numbers are *not* comparable to a
full run's. Marking both degraded would make section 9.2 unreadable; marking neither
would let an incomplete run be quoted as a complete one.

**A tier that is requested but contributes nothing still reports a zero.** A missing row
reads as "not asked for"; a zero reads as "asked for, found nothing". Only the second is
honest while tier 3 is unimplemented.

**Only ambiguity is terminal.** From Tier 2 onward a tier can raise as well as match. An
AMBIGUOUS_SUBSET credit is removed from the residual, because §7.5 reserves that decision
for a human. Every other reason is a capability limit and falls through — treating
POOL_TOO_LARGE as terminal would silently deny Tier 3 the records it exists to handle.

The per-tier count goes to a callback rather than to stdout. A library that prints is a
library that cannot be tested, and the CLI is the right place to decide how a run looks.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from sqlalchemy import Connection

from ledgerloop.audit.provenance import TierResult, record_exception, record_match
from ledgerloop.cascade.tier0_exact import match_tier0
from ledgerloop.cascade.tier1_tolerant import match_tier1
from ledgerloop.cascade.tier2_subsetsum import match_tier2
from ledgerloop.cascade.tier3_llm import match_tier3
from ledgerloop.exceptions.codes import TERMINAL
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.llm.adapter import LLMAdapter
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.store.db import (
    finish_run,
    load_bank_txns,
    load_settlements,
    matched_bank_txn_ids,
    matched_settlement_ids,
)

VALID_TIERS = frozenset({0, 1, 2, 3})

@dataclass(frozen=True)
class TierOutcome:
    """What one tier contributed."""

    tier: int
    matched_bank_txns: int
    matched_settlements: int
    exceptions_raised: int = 0
    llm_invocations: int = 0
    cache_hits: int = 0
    hallucinations: int = 0


@dataclass(frozen=True)
class ReconcileReport:
    """The result of a run, including what it failed to explain.

    ``unmatched_*`` is not decoration: it is the number the next tier inherits and the
    eventual size of the exception queue. Reporting only successes is how a 95% match
    rate ends up quoted against a quietly shrunken denominator.
    """

    run_id: str
    degraded: bool
    tiers: tuple[TierOutcome, ...]
    unmatched_bank_txns: int
    unmatched_settlements: int
    llm_invocations: int = 0
    cache_hits: int = 0
    hallucinations: int = 0


def parse_tiers(spec: str) -> frozenset[int]:
    """Parse an ablation spec such as ``"0,1,2"``.

    Rejects anything unparseable rather than silently running a different configuration
    from the one the resulting metrics will be labelled with.
    """
    tiers: set[int] = set()
    for part in spec.split(","):
        token = part.strip()
        if not token:
            continue
        try:
            tier = int(token)
        except ValueError:
            raise ValueError(f"not a tier number: {token!r}") from None
        if tier not in VALID_TIERS:
            raise ValueError(f"tier out of range: {tier} (expected 0-3)")
        tiers.add(tier)

    if not tiers:
        raise ValueError(f"no tiers selected in {spec!r}")
    return frozenset(tiers)


def reconcile(
    conn: Connection,
    run_id: str,
    *,
    tiers: frozenset[int],
    no_llm: bool = False,
    adapter: LLMAdapter | None = None,
    cache: ResponseCache | None = None,
    on_tier: Callable[[TierOutcome], None] | None = None,
) -> ReconcileReport:
    """Execute the requested tiers in order, posting what each one resolves."""
    bank_txns = load_bank_txns(conn, run_id)
    settlements = load_settlements(conn, run_id)

    # Seeded from what is already posted, so re-running a run cannot double-post.
    claimed_bank = matched_bank_txn_ids(conn, run_id)
    claimed_settlements = matched_settlement_ids(conn, run_id)

    outcomes: list[TierOutcome] = []
    for tier in sorted(tiers):
        residual_bank = [row for row in bank_txns if row.bank_txn_id not in claimed_bank]
        residual_settlements = [
            row for row in settlements if row.settlement_id not in claimed_settlements
        ]

        result, counters = _run_tier(
            tier,
            residual_bank,
            residual_settlements,
            no_llm=no_llm,
            adapter=adapter,
            cache=cache,
        )
        for match in result.matches:
            record_match(conn, run_id, match)
            claimed_bank.add(match.bank_txn_id)
            claimed_settlements.update(match.settlement_ids)

        for raised in result.exceptions:
            record_exception(conn, run_id, raised)
            # Only *terminal* reasons remove a credit from the residual. Ambiguity is
            # terminal by policy — §7.5 forbids the model from resolving it — and its
            # settlements stay unclaimed, because a human may resolve the credit
            # differently. Every other reason is a capability limit: POOL_TOO_LARGE says
            # a deterministic search declined on complexity grounds, and Tier 3 may
            # still resolve it from a handful of pre-filtered candidates.
            if raised.bank_txn_id is not None and raised.code in TERMINAL:
                claimed_bank.add(raised.bank_txn_id)

        outcome = TierOutcome(
            tier=tier,
            matched_bank_txns=len(result.matches),
            matched_settlements=sum(len(match.settlement_ids) for match in result.matches),
            exceptions_raised=len(result.exceptions),
            llm_invocations=counters[0],
            cache_hits=counters[1],
            hallucinations=counters[2],
        )
        outcomes.append(outcome)
        if on_tier is not None:
            on_tier(outcome)

    degraded = no_llm and 3 in tiers
    finish_run(
        conn,
        run_id,
        degraded=degraded,
        tiers_enabled=",".join(str(tier) for tier in sorted(tiers)),
    )

    return ReconcileReport(
        run_id=run_id,
        degraded=degraded,
        tiers=tuple(outcomes),
        unmatched_bank_txns=len(bank_txns) - len(claimed_bank),
        unmatched_settlements=len(settlements) - len(claimed_settlements),
        llm_invocations=sum(outcome.llm_invocations for outcome in outcomes),
        cache_hits=sum(outcome.cache_hits for outcome in outcomes),
        hallucinations=sum(outcome.hallucinations for outcome in outcomes),
    )


def _run_tier(
    tier: int,
    bank_txns: Sequence[BankRow],
    settlements: Sequence[SettlementRow],
    *,
    no_llm: bool,
    adapter: LLMAdapter | None = None,
    cache: ResponseCache | None = None,
) -> tuple[TierResult, tuple[int, int, int]]:
    """Dispatch to a tier, returning its result and its model counters.

    ``--no-llm`` makes Tier 3 run with no adapter rather than skipping it. The
    distinction matters: skipping would leave the residual silently unexamined, while
    running without an adapter raises MODEL_UNAVAILABLE per record — which is what §8
    promises and what makes the degradation visible in the queue instead of invisible.
    """
    empty = (0, 0, 0)
    if tier == 0:
        return TierResult(matches=match_tier0(bank_txns, settlements), exceptions=[]), empty
    if tier == 1:
        return TierResult(matches=match_tier1(bank_txns, settlements), exceptions=[]), empty
    if tier == 2:
        return match_tier2(bank_txns, settlements), empty
    if tier == 3:
        outcome = match_tier3(
            bank_txns,
            settlements,
            adapter=None if no_llm else adapter,
            cache=cache if cache is not None else ResponseCache(None),
        )
        return (
            TierResult(matches=outcome.matches, exceptions=outcome.exceptions),
            (outcome.llm_invocations, outcome.cache_hits, outcome.hallucinations),
        )
    return TierResult(matches=[], exceptions=[]), empty
