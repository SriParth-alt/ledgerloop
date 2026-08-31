"""Tier 3 — constrained LLM adjudication of the residual only.

No test in this file makes a network call. The adapter is scripted, which is not a
compromise but the better instrument: a real model cannot be made to hallucinate an
identifier on demand, and §7.3 requires exactly that failure to be demonstrated live.

What the tier must guarantee, and what each group below checks:

* it sees only the residual — records Tiers 0-2 already settled never reach a model;
* nothing reaches the ledger except through the gates;
* a re-run makes zero calls and produces the same matches, because §7.4 promises
  reconciliation is reproducible for audit;
* when the model is unavailable the batch still completes, degraded, with correctness
  intact (§8).
"""

from __future__ import annotations

import json
from datetime import date

from ledgerloop.cascade.tier3_llm import match_tier3, rank_candidates
from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.exceptions.codes import ExceptionCode
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, PaymentMethod
from ledgerloop.ingest.schemas import BankRow, SettlementRow
from ledgerloop.llm.adapter import ScriptedAdapter
from ledgerloop.llm.cache import ResponseCache
from ledgerloop.llm.prompts.v1 import PROMPT_VERSION

DAY = date(2026, 8, 10)


def bank(
    txn_id: str = "BNK1",
    *,
    credit: int = 300,
    narration: str = "NEFT-CR/HDFC/ACME/BLR",
) -> BankRow:
    return BankRow(
        bank_txn_id=txn_id,
        value_date=DAY,
        narration=narration,
        credit_paise=credit,
        debit_paise=0,
        balance_paise=credit,
    )


def settlement(
    settlement_id: str, *, net: int = 300, name: str = "ACME RETAIL PVT LTD"
) -> SettlementRow:
    return SettlementRow(
        settlement_id=settlement_id,
        payment_id=f"PAY{settlement_id}",
        order_id=f"ORD{settlement_id}",
        invoice_ref=f"INV{settlement_id}",
        customer_name=name,
        method=PaymentMethod.UPI,
        gross_amount_paise=net,
        fee_paise=0,
        gst_on_fee_paise=0,
        tds_paise=0,
        net_amount_paise=net,
        captured_at=DAY,
        settled_on=DAY,
        utr=f"RZRPY{settlement_id[-7:].rjust(7, '0')}",
        status="captured",
    )


def match_response(ids: list[str], *, confidence: float = 0.95) -> str:
    return json.dumps(
        {
            "decision": "MATCH",
            "matched_settlement_ids": ids,
            "confidence": confidence,
            "evidence": [
                {
                    "field_name": "customer_name",
                    "bank_value": "ACME",
                    "settlement_value": "ACME RETAIL PVT LTD",
                    "reasoning": "same counterparty",
                }
            ],
        }
    )


def no_match_response(reason: str = "nothing explains this credit") -> str:
    return json.dumps(
        {
            "decision": "NO_MATCH",
            "matched_settlement_ids": [],
            "confidence": 0.9,
            "evidence": [],
            "unresolved_reason": reason,
        }
    )


def _run(bank_txns, settlements, responses, *, cache=None):
    adapter = ScriptedAdapter(responses=list(responses))
    result = match_tier3(
        bank_txns,
        settlements,
        adapter=adapter,
        cache=cache if cache is not None else ResponseCache(None),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )
    return result, adapter


# =================================================================================
# Candidate selection
# =================================================================================


def test_the_model_never_sees_more_than_the_configured_candidate_cap() -> None:
    """§7.1 caps it at eight. Smaller is safer and cheaper — and a shorter candidate
    list is a smaller surface for the model to pattern-complete over."""
    pool = [settlement(f"STL{i:03d}") for i in range(1, 30)]

    ranked = rank_candidates(
        bank(), pool, config=DEFAULT_MATCH_CONFIG, fee_model=SETTLEMENT_FEE_MODEL
    )

    assert len(ranked) <= DEFAULT_MATCH_CONFIG.max_llm_candidates


def test_candidate_order_is_deterministic() -> None:
    """§7.4 promises a re-run makes zero calls and produces a byte-identical match set.
    That holds only if the prompt is byte-identical, which requires this ordering to be
    stable — otherwise the cache key changes and every re-run pays again."""
    pool = [settlement(f"STL{i:03d}") for i in range(1, 20)]
    kwargs = {"config": DEFAULT_MATCH_CONFIG, "fee_model": SETTLEMENT_FEE_MODEL}
    first = rank_candidates(bank(), pool, **kwargs)
    second = rank_candidates(bank(), pool, **kwargs)

    assert [row.settlement_id for row in first] == [row.settlement_id for row in second]


def test_a_credit_with_no_plausible_candidates_never_reaches_the_model() -> None:
    """Calling a model with an empty candidate list spends money to be told nothing,
    and invites it to invent one."""
    result, adapter = _run([bank(credit=300)], [], [])

    assert adapter.calls == 0
    assert result.matches == []


# =================================================================================
# Posting through the gates
# =================================================================================


def test_an_accepted_proposal_posts_as_a_tier_three_match() -> None:
    result, _ = _run([bank()], [settlement("STL1")], [match_response(["STL1"])])

    assert len(result.matches) == 1
    posted = result.matches[0]
    assert posted.tier == 3
    assert posted.settlement_ids == ("STL1",)
    assert posted.confidence >= 0.85
    assert posted.evidence, "a tier-3 match must carry the model's evidence"


def test_a_hallucinated_id_is_rejected_and_counted() -> None:
    """The failure §7.3 demos live. The response is discarded whole, an exception is
    raised, and the counter moves so the run metrics can report it."""
    result, _ = _run([bank()], [settlement("STL1")], [match_response(["STL_GHOST"])])

    assert result.matches == []
    assert [item.code for item in result.exceptions] == [ExceptionCode.LLM_INVALID_OUTPUT]
    assert result.hallucinations == 1


def test_a_proposal_whose_arithmetic_fails_is_rejected_however_confident() -> None:
    """The model never overrides arithmetic. Confidence 0.99 does not buy a match whose
    numbers do not work."""
    result, _ = _run(
        [bank(credit=999_999)],
        [settlement("STL1", net=300)],
        [match_response(["STL1"], confidence=0.99)],
    )

    assert result.matches == []
    assert [item.code for item in result.exceptions] == [ExceptionCode.AMOUNT_BEYOND_TOLERANCE]


def test_a_low_confidence_proposal_becomes_a_queue_item() -> None:
    result, _ = _run([bank()], [settlement("STL1")], [match_response(["STL1"], confidence=0.5)])

    assert result.matches == []
    assert [item.code for item in result.exceptions] == [ExceptionCode.LOW_CONFIDENCE]


def test_a_declined_credit_carries_the_models_reason_into_the_queue() -> None:
    """A NO_MATCH is the model being useful. Its reason is what a human reads first."""
    result, _ = _run(
        [bank()], [settlement("STL1")], [no_match_response("narration names a different payer")]
    )

    assert result.matches == []
    assert [item.code for item in result.exceptions] == [ExceptionCode.NO_CANDIDATE]
    assert "different payer" in str(result.exceptions[0].detail)


def test_a_malformed_response_does_not_stop_the_batch() -> None:
    """§8: one bad response costs one record, not the run. The second credit must still
    be adjudicated."""
    result, adapter = _run(
        [bank("BNK1"), bank("BNK2")],
        [settlement("STL1"), settlement("STL2")],
        ["{ not json", match_response(["STL2"])],
    )

    assert adapter.calls == 2
    assert len(result.matches) == 1
    assert any(item.code is ExceptionCode.LLM_INVALID_OUTPUT for item in result.exceptions)


# =================================================================================
# Provenance
# =================================================================================


def test_every_tier_three_match_names_the_model_and_prompt_version() -> None:
    """§7.4: a prompt change must be visible in the trail, so the version travels with
    the match rather than being looked up from whatever the code says later."""
    result, _ = _run([bank()], [settlement("STL1")], [match_response(["STL1"])])

    assert result.model_name == "scripted"
    assert result.prompt_version == PROMPT_VERSION


# =================================================================================
# Caching and reproducibility
# =================================================================================


def test_a_cached_response_makes_no_call(tmp_path) -> None:
    """§7.4. A re-run of the same batch performs zero new API calls."""
    cache = ResponseCache(tmp_path)
    _, first_adapter = _run(
        [bank()], [settlement("STL1")], [match_response(["STL1"])], cache=cache
    )
    second, second_adapter = _run([bank()], [settlement("STL1")], [], cache=cache)

    assert first_adapter.calls == 1
    assert second_adapter.calls == 0, "the second run must be served entirely from cache"
    assert second.cache_hits == 1


def test_a_re_run_produces_an_identical_match_set(tmp_path) -> None:
    """Reconciliation must be reproducible for audit. Two runs over one batch must not
    disagree about which money was matched."""
    cache = ResponseCache(tmp_path)
    args = ([bank()], [settlement("STL1")])
    first, _ = _run(*args, [match_response(["STL1"])], cache=cache)
    second, _ = _run(*args, [], cache=cache)

    assert [(m.bank_txn_id, m.settlement_ids) for m in first.matches] == [
        (m.bank_txn_id, m.settlement_ids) for m in second.matches
    ]


def test_a_different_candidate_set_is_a_different_cache_entry(tmp_path) -> None:
    """The key covers the whole prompt payload. Reusing a response across a changed
    candidate list would answer a question the model was never asked."""
    cache = ResponseCache(tmp_path)
    _run([bank()], [settlement("STL1")], [match_response(["STL1"])], cache=cache)
    _, adapter = _run(
        [bank()],
        [settlement("STL1"), settlement("STL2", net=301)],
        [no_match_response()],
        cache=cache,
    )

    assert adapter.calls == 1


# =================================================================================
# Degradation
# =================================================================================


def test_no_adapter_completes_the_batch_degraded_rather_than_failing() -> None:
    """§8's promise, and the one that matters most for a live demo: if the model is
    unavailable the batch finishes, the auto-match rate falls, and correctness does not.
    """
    result = match_tier3(
        [bank()],
        [settlement("STL1")],
        adapter=None,
        cache=ResponseCache(None),
        fee_model=SETTLEMENT_FEE_MODEL,
        config=DEFAULT_MATCH_CONFIG,
    )

    assert result.matches == []
    assert [item.code for item in result.exceptions] == [ExceptionCode.MODEL_UNAVAILABLE]
    assert result.llm_invocations == 0
