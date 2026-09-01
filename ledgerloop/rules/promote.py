"""Promote a human resolution into a reusable rule. This is the agentic loop.

When a human resolves an exception, the system inspects the resolution and proposes a
GENERALISED rule in both natural language and machine-readable form. On approval it is
persisted to ``rules/store.yaml`` and replayed on the next run.

Measure the lift: auto-match rate before and after promoting five rules. That delta is
the evidence the loop does something, rather than being a UI flourish.

**One resolution, one hypothesis.** A resolution is a single data point. The promoter
asks one question — *why did the cascade miss this?* — and proposes exactly one rule.
Inferring several from one example is how a store fills with overfitted guesses that
nobody approved individually.

**Not every resolution contains a lesson.** A genuinely unique credit a human matched on
judgement yields no rule at all. Proposing one anyway would fire on the next batch and
create false matches from a sample of one.

**Approval is the only gate.** ``propose_rule`` persists nothing. A bad generalisation
can create false matches at scale, and ADR-004 named human approval as the sole thing
standing in front of that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml
from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import Connection, text

from ledgerloop.ingest.schemas import BankRow, SettlementRow

#: A promoted prefix must still leave a plausible reference behind. Tier 0 posts at
#: confidence 1.0, so a rule that whittled short tokens into references that were never
#: in the file would be the most expensive kind of learning.
MIN_PREFIX_REMAINDER = 8

#: Below this, two spellings are different customers rather than one written twice.
ALIAS_SIMILARITY_FLOOR = 55

#: A gap smaller than this is paise drift, not a pricing error. Proposing a rate change
#: from rounding noise would teach the system a wrong rate from a correct match.
_FEE_DRIFT_NOISE_FLOOR = 500

#: Bounds on a margin worth believing. Below the floor it is arithmetic noise; above
#: the ceiling the resolution is explained by something other than pricing, and solving
#: for a rate anyway produces a confident number from a misunderstanding.
_MIN_LEARNABLE_DRIFT_BPS = 10
_MAX_LEARNABLE_DRIFT_BPS = 500

_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")
_FIELD_SPLIT = re.compile(r"[/|]")


class RuleKind(StrEnum):
    """What a rule can teach the cascade.

    Deliberately two. Twelve kinds would be a worse demonstration than two that visibly
    work, and §9.3 asks for a measured delta rather than coverage.
    """

    NARRATION_PREFIX = "narration_prefix"
    """An instrument prefix this bank glues onto references, which the normaliser did
    not know about. Fixes an entire class of credits from that bank."""

    COUNTERPARTY_ALIAS = "counterparty_alias"
    """A spelling that denotes a customer already known by another name."""

    FEE_OVERRIDE = "fee_override"
    """How far this customer's real MDR sits from our configured one, in basis points.

    The most valuable of the three, and the one §8 is really about. A wrong fee model
    does not break one record — it declines every settlement for that merchant, which is
    why the queue clusters and why a single approved rule repairs the whole class."""


@dataclass(frozen=True)
class Rule:
    """A generalisation, in a form a human can approve and a machine can run."""

    kind: RuleKind
    value: str
    description: str
    learned_from: str


@dataclass(frozen=True)
class Resolution:
    """What a human decided: this credit is explained by this settlement."""

    bank_txn: BankRow
    settlement: SettlementRow
    resolved_by: str


@dataclass(frozen=True)
class RuleStore:
    """Everything the system has been taught and had approved."""

    narration_prefixes: tuple[str, ...] = ()
    aliases: dict[str, str] = None  # type: ignore[assignment]
    fee_rates_bps: dict[str, int] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.aliases is None:
            object.__setattr__(self, "aliases", {})
        if self.fee_rates_bps is None:
            object.__setattr__(self, "fee_rates_bps", {})

    @property
    def is_empty(self) -> bool:
        return not self.narration_prefixes and not self.aliases and not self.fee_rates_bps

    def alias_for(self, name: str) -> str | None:
        return self.aliases.get(name.strip().upper())

    def fee_drift_for(self, customer_name: str) -> int | None:
        """The learned margin between this customer's real MDR and our configured one."""
        return self.fee_rates_bps.get(customer_name.strip().upper())


EMPTY_STORE = RuleStore()


def _normalise(token: str) -> str:
    return _NON_ALPHANUMERIC.sub("", token.upper())


def propose_rule(resolution: Resolution) -> Rule | None:
    """Work out why the cascade missed this, and generalise it. Persists nothing.

    Two hypotheses are considered, and the stronger one wins. A reference recovered by
    stripping an unknown prefix is the stronger signal — it is exact, it is attributable
    to a bank rather than to a customer, and it fixes every future credit carrying that
    prefix. A name variant is weaker evidence and is only proposed when no prefix
    explains the miss.
    """
    override = _infer_fee_rate(resolution)
    if override is not None:
        customer, rate_bps = override
        return Rule(
            kind=RuleKind.FEE_OVERRIDE,
            value=f"{customer}={rate_bps}",
            description=(
                f"The bank credited {resolution.bank_txn.credit_paise} paise where our fee "
                f"model predicted a different net for {customer}. Solving for the margin "
                f"that reconciles them gives {rate_bps:+d} bps against our configured "
                f"schedule. Our pricing is wrong for this merchant, which declines every "
                f"one of their settlements — correcting it resolves the whole class. "
                f"Learned from {resolution.bank_txn.bank_txn_id}."
            ),
            learned_from=resolution.bank_txn.bank_txn_id,
        )

    prefix = _infer_prefix(resolution)
    if prefix is not None:
        return Rule(
            kind=RuleKind.NARRATION_PREFIX,
            value=prefix,
            description=(
                f"The narration glued '{prefix}' onto the reference, so the normaliser "
                f"could not recognise it. Strip '{prefix}' from reference-shaped tokens "
                f"and every future credit carrying it resolves without a human. Learned "
                f"from {resolution.bank_txn.bank_txn_id}."
            ),
            learned_from=resolution.bank_txn.bank_txn_id,
        )

    alias = _infer_alias(resolution)
    if alias is not None:
        spelling, canonical = alias
        return Rule(
            kind=RuleKind.COUNTERPARTY_ALIAS,
            value=f"{spelling}={canonical}",
            description=(
                f"The narration named this customer '{spelling}' where the gateway calls "
                f"them '{canonical}'. Treating the two as one name lets the counterparty "
                f"signal fire on future credits. Learned from "
                f"{resolution.bank_txn.bank_txn_id}."
            ),
            learned_from=resolution.bank_txn.bank_txn_id,
        )

    # Not every resolution contains a lesson. A one-off matched on human judgement
    # generalises to nothing, and inventing a rule from it would fire on the next batch.
    return None


def _infer_fee_rate(resolution: Resolution) -> tuple[str, int] | None:
    """Solve for how far this merchant's real MDR sits from our configured one.

    Considered first, because it is the strongest signal of the three. A prefix or an
    alias explains one narration; a wrong rate explains every settlement for a merchant,
    and section 8's whole argument is that such a cluster is one problem rather than many.

    **A drift, not an absolute rate.** Learning "this customer's rate is 140 bps" from a
    debit-card settlement and applying it to their UPI settlements is wrong by the whole
    difference between the two schedules, and it breaks matches that previously worked.
    A merchant negotiates a *schedule* — every method moves together — so the learnable
    quantity is the margin, and it generalises across methods honestly.

    The arithmetic runs the fee model backwards. With ``net = gross - fee - gst(fee) -
    tds`` the fee implied by an observed net is ``(gross - tds - net) / (1 + gst)``, and
    the rate is that fee as a share of gross.

    Three guards, and each exists because its absence produced a wrong rule:

    * only a clean ``captured`` settlement qualifies. A partial refund nets against the
      same cycle and the fee model cannot see it, so the gap looks exactly like a pricing
      error and yields an invented rate for a merchant whose pricing is fine;
    * the gap must exceed a noise floor, so paise drift is never read as a rate change;
    * the implied margin must be plausible. A resolution the human got right but that this
      arithmetic cannot explain should yield nothing rather than a confident wrong number.
    """
    from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL

    settlement = resolution.settlement
    gross = settlement.gross_amount_paise
    if gross <= 0 or settlement.status != "captured":
        return None

    modelled = SETTLEMENT_FEE_MODEL.net_paise(gross, settlement.method)
    observed = resolution.bank_txn.credit_paise
    if abs(observed - modelled) <= max(_FEE_DRIFT_NOISE_FLOOR, gross // 1000):
        return None

    tds = SETTLEMENT_FEE_MODEL.tds_paise(gross)
    gst_multiplier = 10_000 + SETTLEMENT_FEE_MODEL.gst_bps
    implied_fee = ((gross - tds - observed) * 10_000) // gst_multiplier
    if implied_fee <= 0:
        return None

    implied_bps = round(implied_fee * 10_000 / gross)
    configured = SETTLEMENT_FEE_MODEL.pricing[settlement.method].rate_bps
    drift = implied_bps - configured
    if not _MIN_LEARNABLE_DRIFT_BPS <= abs(drift) <= _MAX_LEARNABLE_DRIFT_BPS:
        return None

    return settlement.customer_name.strip().upper(), drift


def _infer_prefix(resolution: Resolution) -> str | None:
    """Find a prefix that, once stripped, reveals the settlement's reference."""
    reference = resolution.settlement.utr
    if not reference:
        return None
    target = _normalise(reference)

    for field in _FIELD_SPLIT.split(resolution.bank_txn.narration):
        token = _normalise(field)
        if token == target or not token.endswith(target):
            continue
        prefix = token[: -len(target)]
        if prefix and len(target) >= MIN_PREFIX_REMAINDER:
            return prefix
    return None


def _infer_alias(resolution: Resolution) -> tuple[str, str] | None:
    """Find the narration field that names this customer under another spelling."""
    canonical = resolution.settlement.customer_name.strip().upper()

    best: tuple[int, str] | None = None
    for field in _FIELD_SPLIT.split(resolution.bank_txn.narration):
        spelling = field.strip().upper()
        if not spelling or spelling == canonical:
            continue
        score = round(token_set_ratio(canonical, spelling))
        if score >= ALIAS_SIMILARITY_FLOOR and (best is None or score > best[0]):
            best = (score, spelling)

    return (best[1], canonical) if best is not None else None


def load_rules(path: Path) -> RuleStore:
    """Read the approved rules. A missing store is an empty one, not an error.

    ``rules/store.yaml`` ships with ``rules: []`` on purpose: committing it means a
    reviewer can diff the file after a demo and see exactly what the system learned.
    """
    path = Path(path)
    if not path.exists():
        return EMPTY_STORE

    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    prefixes: list[str] = []
    aliases: dict[str, str] = {}
    fee_rates: dict[str, int] = {}

    for entry in document.get("rules") or []:
        kind = entry.get("kind")
        value = entry.get("value", "")
        if kind == RuleKind.NARRATION_PREFIX.value and value not in prefixes:
            prefixes.append(value)
        elif kind == RuleKind.COUNTERPARTY_ALIAS.value and "=" in value:
            spelling, canonical = value.split("=", 1)
            aliases[spelling.strip().upper()] = canonical.strip().upper()
        elif kind == RuleKind.FEE_OVERRIDE.value and "=" in value:
            customer, rate = value.split("=", 1)
            fee_rates[customer.strip().upper()] = int(rate)

    return RuleStore(
        narration_prefixes=tuple(prefixes), aliases=aliases, fee_rates_bps=fee_rates
    )


def promote(rule: Rule, path: Path, *, approved_by: str) -> None:
    """Persist an approved rule. This is the only function that writes.

    Re-promoting an identical rule is a no-op. The same class of exception recurs across
    runs, each resolution proposes the same rule, and a store that grew a copy per
    resolution would slow every later run while learning nothing new.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, Any] = {}
    if path.exists():
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    existing: list[dict[str, Any]] = document.get("rules") or []

    if any(
        entry.get("kind") == rule.kind.value and entry.get("value") == rule.value
        for entry in existing
    ):
        return

    existing.append(
        {
            "kind": rule.kind.value,
            "value": rule.value,
            "description": rule.description,
            "learned_from": rule.learned_from,
            "approved_by": approved_by,
            "approved_at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    )
    path.write_text(
        yaml.safe_dump({"rules": existing}, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )


def record_resolution(
    conn: Connection,
    run_id: str,
    exception_id: str,
    *,
    settlement_id: str,
    resolved_by: str,
) -> Resolution:
    """Record that a human decided this credit is explained by this settlement.

    Marks the exception resolved and returns what was decided, so a rule can be proposed
    from it. It deliberately does **not** promote: a resolution is a judgement about one
    record, while a rule fires forever on batches nobody has looked at, and ADR-004 makes
    human approval the only gate between the two.
    """
    row = conn.execute(
        text(
            "SELECT bank_txn_id FROM exceptions "
            "WHERE run_id = :run AND exception_id = :id"
        ),
        {"run": run_id, "id": exception_id},
    ).first()
    if row is None:
        raise KeyError(f"no exception {exception_id!r} in run {run_id!r}")

    bank_row = conn.execute(
        text(
            "SELECT bank_txn_id, value_date, narration, credit_paise, debit_paise, "
            "balance_paise FROM bank_txns WHERE run_id = :run AND bank_txn_id = :id"
        ),
        {"run": run_id, "id": row.bank_txn_id},
    ).mappings().first()
    if bank_row is None:
        raise KeyError(f"exception {exception_id!r} names no bank row")

    settlement_row = conn.execute(
        text(
            "SELECT settlement_id, payment_id, order_id, invoice_ref, customer_name, "
            "method, gross_amount_paise, fee_paise, gst_on_fee_paise, tds_paise, "
            "net_amount_paise, captured_at, settled_on, utr, status FROM settlements "
            "WHERE run_id = :run AND settlement_id = :id"
        ),
        {"run": run_id, "id": settlement_id},
    ).mappings().first()
    if settlement_row is None:
        raise KeyError(f"no settlement {settlement_id!r} in run {run_id!r}")

    conn.execute(
        text(
            "UPDATE exceptions SET resolved_at = :at, resolved_by = :by, "
            "resolution_json = :detail WHERE exception_id = :id"
        ),
        {
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
            "by": resolved_by,
            "detail": json.dumps(
                {"settlement_id": settlement_id, "bank_txn_id": row.bank_txn_id}
            ),
            "id": exception_id,
        },
    )

    return Resolution(
        bank_txn=BankRow.model_validate(dict(bank_row)),
        settlement=SettlementRow.model_validate(dict(settlement_row)),
        resolved_by=resolved_by,
    )


def attach_promoted_rule(conn: Connection, exception_id: str, rule_id: str) -> None:
    """Link the rule back to the decision that produced it.

    This is the audit trail for the loop itself: which human decision, on which record,
    produced which rule. Without it a store entry is a rule nobody can trace to a reason.
    """
    conn.execute(
        text("UPDATE exceptions SET promoted_rule_id = :rule WHERE exception_id = :id"),
        {"rule": rule_id, "id": exception_id},
    )
