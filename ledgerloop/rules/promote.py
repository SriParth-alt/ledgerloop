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

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import yaml
from rapidfuzz.fuzz import token_set_ratio

from ledgerloop.ingest.schemas import BankRow, SettlementRow

#: A promoted prefix must still leave a plausible reference behind. Tier 0 posts at
#: confidence 1.0, so a rule that whittled short tokens into references that were never
#: in the file would be the most expensive kind of learning.
MIN_PREFIX_REMAINDER = 8

#: Below this, two spellings are different customers rather than one written twice.
ALIAS_SIMILARITY_FLOOR = 55

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

    def __post_init__(self) -> None:
        if self.aliases is None:
            object.__setattr__(self, "aliases", {})

    @property
    def is_empty(self) -> bool:
        return not self.narration_prefixes and not self.aliases

    def alias_for(self, name: str) -> str | None:
        return self.aliases.get(name.strip().upper())


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

    for entry in document.get("rules") or []:
        kind = entry.get("kind")
        value = entry.get("value", "")
        if kind == RuleKind.NARRATION_PREFIX.value and value not in prefixes:
            prefixes.append(value)
        elif kind == RuleKind.COUNTERPARTY_ALIAS.value and "=" in value:
            spelling, canonical = value.split("=", 1)
            aliases[spelling.strip().upper()] = canonical.strip().upper()

    return RuleStore(narration_prefixes=tuple(prefixes), aliases=aliases)


def promote(rule: Rule, path: Path, *, approved_by: str) -> None:
    """Persist an approved rule. This is the only function that writes.

    Re-promoting an identical rule is a no-op. The same class of exception recurs across
    runs, each resolution proposes the same rule, and a store that grew a copy per
    resolution would slow every later run while learning nothing new.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    document = {}
    if path.exists():
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    existing = document.get("rules") or []

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
