"""Tier 3 — constrained LLM adjudication of the residual only.

Assembles at most ``max_llm_candidates`` pre-scored candidates, calls the adapter at
temperature 0, then runs every response through ``cascade.gates.run_all_gates``. Nothing
reaches the ledger except through the gates.

The model receives: one bank row (raw narration included), the candidates, the fee model
and the date window as prose, and a strict output schema. It returns an Adjudication or
it returns nothing usable. It does not compute amounts. It does not resolve ambiguity.
It does not see records that Tiers 0-2 already settled.

**What this tier is not.** It is one schema-constrained call per residual record. There
is no planning, no tool use, no loop, no autonomy. That is deliberate and it is ADR-002's
whole argument — the agentic surface of this project is the exception-to-rule promotion
loop, not the cascade. If someone points at this tier and calls it the agent, the thesis
has been misread.

**Degradation is a first-class path.** With no adapter and no cached response the batch
completes, every affected record becomes MODEL_UNAVAILABLE, and the run is marked
degraded. Auto-match rate falls; correctness does not (§8).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ledgerloop.audit.provenance import (
    MatchEvidence,
    ProposedException,
    ProposedMatch,
)
from ledgerloop.cascade.gates import run_all_gates
from ledgerloop.cascade.tier1_tolerant import name_similarity, reference_distance
from ledgerloop.cascade.tier2_subsetsum import candidate_pool
from ledgerloop.config import DEFAULT_MATCH_CONFIG, MatchConfig
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, FeeModel
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.llm.adapter import DEFAULT_MODEL, LLMAdapter
from ledgerloop.llm.cache import ResponseCache, cache_key
from ledgerloop.llm.prompts.v1 import PROMPT_VERSION, render

RULE_ADJUDICATED = "T3-ADJUDICATED"

TIER = 3


@dataclass(frozen=True)
class Tier3Result:
    """What Tier 3 decided, plus the counters §9.1 reports.

    The counters are returned rather than derived from the store afterwards, because
    "how many times did we call a model" is not recoverable from the matches alone — a
    NO_MATCH response costs a call and leaves no match behind.
    """

    matches: list[ProposedMatch] = field(default_factory=list)
    exceptions: list[ProposedException] = field(default_factory=list)
    llm_invocations: int = 0
    cache_hits: int = 0
    hallucinations: int = 0
    model_name: str | None = None
    prompt_version: str = PROMPT_VERSION


def rank_candidates(
    bank_txn: BankRow,
    settlements: Sequence[SettlementRow],
    *,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
) -> list[SettlementRow]:
    """The at-most-eight candidates the model is allowed to see.

    Filtered by the same date window Tier 2 uses, then ranked by the two signals Tier 1
    scores with — reference proximity and counterparty name. The *amount* gate is
    deliberately not applied: a record reaching Tier 3 is one whose amount did not
    reconcile cleanly, so filtering on it here would empty the candidate list precisely
    when the tier is needed.

    Sorted by identifier after ranking. §7.4 promises a re-run makes zero API calls, and
    that only holds if the prompt is byte-identical, which requires this order to be
    stable across runs.
    """
    pool = candidate_pool(bank_txn, settlements, fee_model=fee_model, config=config)

    def strength(row: SettlementRow) -> tuple[int, int, str]:
        distance = reference_distance(bank_txn.narration, row.utr)
        # Absent evidence ranks below weak evidence, never above it.
        reference_score = 0 if distance is None else max(0, 64 - distance)
        return (
            -reference_score,
            -name_similarity(row.customer_name, bank_txn.narration),
            row.settlement_id,
        )

    ranked = sorted(pool, key=strength)[: config.max_llm_candidates]
    return sorted(ranked, key=lambda row: row.settlement_id)


def effective_model_name(adapter: LLMAdapter | None, configured: str) -> str:
    """Who is answering: the live adapter if there is one, else the configured model.

    Extracted so the cache key and the provenance record cannot drift apart. They are the
    same question — *which model produced this answer* — and answering it differently in
    two places is how the cache came to be unreadable without an API key (ADR-035).
    """
    return adapter.name if adapter is not None else configured


def match_tier3(
    bank_txns: Sequence[BankRow],
    settlements: Sequence[SettlementRow],
    *,
    adapter: LLMAdapter | None,
    cache: ResponseCache,
    model_name: str = DEFAULT_MODEL,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    config: MatchConfig = DEFAULT_MATCH_CONFIG,
) -> Tier3Result:
    """Adjudicate the residual, one credit at a time, through the gates.

    ``model_name`` is the identity the cache is keyed under **when no adapter is
    present**, and it is why a run with no API key can still reproduce Tier 3.

    Previously this was read solely off ``adapter.name``, so a keyless run computed its
    keys under the empty string and missed every committed response — the cache that
    exists specifically to make Tier 3 reproducible without a key only worked for people
    who had one. It also wrote ``""`` into the provenance record.

    A live adapter still wins, because provenance must name whoever actually answered: a
    scripted response did not come from Gemini, and recording that it did would be a false
    statement in the one table the audit trail rests on. In production the two agree —
    ``build_adapter`` is given the same pinned model — so the cache stays warm either way.
    """
    # Whoever actually answers is the model of record. Falling back to the configured
    # name (rather than to None) is what lets a keyless run hit the committed cache.
    model_name = effective_model_name(adapter, model_name)

    matches: list[ProposedMatch] = []
    exceptions: list[ProposedException] = []
    invocations = 0
    hits = 0
    hallucinations = 0
    claimed: set[str] = set()

    for bank_txn in bank_txns:
        available = [row for row in settlements if row.settlement_id not in claimed]
        candidates = rank_candidates(
            bank_txn, available, config=config, fee_model=fee_model
        )
        if not candidates:
            # Calling a model with an empty candidate list spends money to be told
            # nothing, and invites it to invent one.
            continue

        prompt = render(
            bank_txn,
            candidates,
            fee_model=fee_model,
            slack_days=config.settlement_slack_days,
        )
        key = cache_key(prompt, model=model_name or "")

        raw = cache.get(key)
        if raw is not None:
            hits += 1
        elif adapter is None:
            exceptions.append(
                ProposedException(
                    code=ExceptionCode.MODEL_UNAVAILABLE,
                    bank_txn_id=bank_txn.bank_txn_id,
                    settlement_id=None,
                    value_at_risk_paise=bank_txn.credit_paise,
                    detail={
                        "note": (
                            "No model was reachable and no cached response exists. The "
                            "batch completed without Tier 3; auto-match rate falls, "
                            "correctness does not."
                        )
                    },
                )
            )
            continue
        else:
            raw = adapter.complete(prompt)
            invocations += 1
            cache.put(key, raw)

        settlements_by_id = {row.settlement_id: row for row in candidates}
        outcome = run_all_gates(
            raw,
            frozenset(settlements_by_id),
            bank_txn.credit_paise,
            bank_txn.value_date,
            settlements_by_id,
            fee_model=fee_model,
            config=config,
        )

        if outcome.hallucinated_ids:
            hallucinations += len(outcome.hallucinated_ids)

        if not outcome.accepted:
            exceptions.append(_rejected(bank_txn, outcome))
            continue

        adjudication = outcome.adjudication
        assert adjudication is not None  # an accepted gate result always carries one

        if adjudication.decision == "NO_MATCH":
            exceptions.append(_declined(bank_txn, adjudication.unresolved_reason))
            continue

        members = tuple(adjudication.matched_settlement_ids)
        matches.append(
            ProposedMatch(
                bank_txn_id=bank_txn.bank_txn_id,
                settlement_ids=members,
                tier=TIER,
                rule_id=RULE_ADJUDICATED,
                confidence=adjudication.confidence,
                evidence=tuple(
                    MatchEvidence(
                        field=item.field_name,
                        bank_value=item.bank_value,
                        settlement_value=item.settlement_value,
                        note=item.reasoning,
                    )
                    for item in adjudication.evidence
                ),
            )
        )
        claimed.update(members)

    return Tier3Result(
        matches=matches,
        exceptions=exceptions,
        llm_invocations=invocations,
        cache_hits=hits,
        hallucinations=hallucinations,
        model_name=model_name,
        prompt_version=PROMPT_VERSION,
    )


def _rejected(bank_txn: BankRow, outcome: object) -> ProposedException:
    """Turn a gate rejection into a queue item that says which gate and why."""
    code = getattr(outcome, "exception_code", None) or ExceptionCode.LLM_INVALID_OUTPUT
    detail: dict[str, object] = {"note": getattr(outcome, "detail", "")}
    hallucinated = getattr(outcome, "hallucinated_ids", ())
    if hallucinated:
        detail["hallucinated_ids"] = list(hallucinated)
    return ProposedException(
        code=code,
        bank_txn_id=bank_txn.bank_txn_id,
        settlement_id=None,
        value_at_risk_paise=bank_txn.credit_paise,
        detail=detail,
    )


def _declined(bank_txn: BankRow, reason: str | None) -> ProposedException:
    """A NO_MATCH is the model being useful. Its reason is what a human reads first."""
    return ProposedException(
        code=ExceptionCode.NO_CANDIDATE,
        bank_txn_id=bank_txn.bank_txn_id,
        settlement_id=None,
        value_at_risk_paise=bank_txn.credit_paise,
        detail={"note": reason or "model found no candidate that explains this credit"},
    )
