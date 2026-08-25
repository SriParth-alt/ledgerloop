"""Tier 0 — exact deterministic matching. No LLM.

Normalises UTRs (uppercase, strip non-alphanumerics, drop NEFT/IMPS/RTGS/CR/UPI
prefixes), extracts candidate tokens from narration by length and charset, then matches
on exact normalised UTR or on an exactly-unique ``(net_amount_paise, value_date)`` pair.

UNIQUENESS GUARD: if a key maps to more than one candidate on either side, it does not
match here. Fall through. Tier 0 exists to be unimpeachable.

This module is deliberately pure — it takes rows, returns proposals, and touches no
database and no network. Hard rule 1 (tiers 0-2 never call a model) is therefore
auditable by reading the imports, and a difficult case can be constructed in four lines
instead of being staged through a CSV.

**Known limitation, stated plainly.** The amount-and-date rule is the only place in
tiers 0-2 that can post a wrong match. If a batched credit's total coincidentally
equals some unrelated settlement's net on the same date, and each key happens to be
unique on its own side, this tier posts at confidence 1.0 and nothing later revisits
it. The uniqueness guard does not close that hole — it cannot, because the tier has no
way to know a credit was batched. That is inherent to the rule as specified in §6, and
the eval harness exists to measure exactly this. If day 8 reports a non-zero false-match
rate, this rule is the first thing to look at.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from datetime import date

from ledgerloop.audit.provenance import MatchEvidence, ProposedMatch
from ledgerloop.ingest.schemas import BankRow, SettlementRow

RULE_UTR_EXACT = "T0-UTR-EXACT"
RULE_AMOUNT_DATE_UNIQUE = "T0-AMOUNT-DATE-UNIQUE"

TIER = 0
CONFIDENCE = 1.0

#: Instrument prefixes banks glue onto a reference. §6 names these.
BANK_PREFIXES = ("NEFT", "IMPS", "RTGS", "UPI", "CR", "DR")

#: A reference must be at least this long to be a candidate, and stripping a prefix may
#: never take a token below it. Whittling a short token would manufacture a reference
#: that was never in the file — and this tier posts at confidence 1.0.
MIN_REFERENCE_LENGTH = 8
MAX_REFERENCE_LENGTH = 32

_NON_ALPHANUMERIC = re.compile(r"[^A-Z0-9]")

#: Narration fields are separated by '/'. Splitting on '-' as well would destroy every
#: delimiter-varied reference: NARRATION_NOISE injects a hyphen *inside* the token, so
#: 'RZRPY3-482986' would become two fragments that match nothing.
_FIELD_SPLIT = re.compile(r"[/|]")


def normalise_utr(token: str) -> str:
    """Uppercase, strip non-alphanumerics, then drop glued bank prefixes."""
    normalised = _NON_ALPHANUMERIC.sub("", token.upper())

    changed = True
    while changed:
        changed = False
        for prefix in BANK_PREFIXES:
            remainder = normalised[len(prefix) :]
            if (
                normalised.startswith(prefix)
                and len(remainder) >= MIN_REFERENCE_LENGTH
            ):
                normalised = remainder
                changed = True
                break

    return normalised


def _looks_like_reference(token: str) -> bool:
    """A reference carries at least one digit and is long enough to be distinctive.

    Requiring a digit is what keeps customer names out. A purely alphabetic token would
    otherwise let Tier 0 match two unrelated rows that merely share a counterparty.
    """
    return (
        MIN_REFERENCE_LENGTH <= len(token) <= MAX_REFERENCE_LENGTH
        and token.isalnum()
        and any(character.isdigit() for character in token)
    )


def utr_candidates(narration: str) -> frozenset[str]:
    """Every reference-shaped token a narration might be hiding."""
    tokens = set()
    for field in _FIELD_SPLIT.split(narration):
        normalised = normalise_utr(field)
        if _looks_like_reference(normalised):
            tokens.add(normalised)
    return frozenset(tokens)


def _unique_by(pairs: list[tuple[object, str]]) -> dict[object, str]:
    """Keep only keys that map to exactly one identifier.

    This is the uniqueness guard. A key claimed by two rows is ambiguous, and ambiguity
    is never resolved by guessing.
    """
    grouped: dict[object, list[str]] = defaultdict(list)
    for key, identifier in pairs:
        grouped[key].append(identifier)
    return {key: values[0] for key, values in grouped.items() if len(values) == 1}


def _match_on_reference(
    bank_txns: Sequence[BankRow], settlements: Sequence[SettlementRow]
) -> list[ProposedMatch]:
    settlement_by_utr = _unique_by(
        [
            (normalise_utr(row.utr), row.settlement_id)
            for row in settlements
            if row.utr and normalise_utr(row.utr)
        ]
    )

    bank_pairs: list[tuple[object, str]] = []
    candidates_by_bank: dict[str, frozenset[str]] = {}
    for row in bank_txns:
        candidates = utr_candidates(row.narration)
        candidates_by_bank[row.bank_txn_id] = candidates
        bank_pairs.extend((token, row.bank_txn_id) for token in candidates)
    bank_by_utr = _unique_by(bank_pairs)

    matches: list[ProposedMatch] = []
    for token, settlement_id in sorted(settlement_by_utr.items(), key=lambda item: str(item[0])):
        bank_txn_id = bank_by_utr.get(token)
        if bank_txn_id is None:
            continue
        matches.append(
            ProposedMatch(
                bank_txn_id=bank_txn_id,
                settlement_ids=(settlement_id,),
                tier=TIER,
                rule_id=RULE_UTR_EXACT,
                confidence=CONFIDENCE,
                evidence=(
                    MatchEvidence(
                        field="narration_token",
                        bank_value=str(token),
                        settlement_value=str(token),
                        note="normalised reference matched exactly and uniquely on both sides",
                    ),
                ),
            )
        )
    return matches


def _match_on_amount_and_date(
    bank_txns: Sequence[BankRow], settlements: Sequence[SettlementRow]
) -> list[ProposedMatch]:
    settlement_by_key = _unique_by(
        [
            ((row.net_amount_paise, row.settled_on), row.settlement_id)
            for row in settlements
        ]
    )
    bank_by_key = _unique_by(
        [((row.credit_paise, row.value_date), row.bank_txn_id) for row in bank_txns]
    )

    matches: list[ProposedMatch] = []
    for key, settlement_id in sorted(
        settlement_by_key.items(), key=lambda item: (item[0][0], item[0][1])  # type: ignore[index]
    ):
        bank_txn_id = bank_by_key.get(key)
        if bank_txn_id is None:
            continue
        amount, settled_on = key  # type: ignore[misc]
        matches.append(
            ProposedMatch(
                bank_txn_id=bank_txn_id,
                settlement_ids=(settlement_id,),
                tier=TIER,
                rule_id=RULE_AMOUNT_DATE_UNIQUE,
                confidence=CONFIDENCE,
                evidence=(
                    MatchEvidence(
                        field="net_amount_paise",
                        bank_value=str(amount),
                        settlement_value=str(amount),
                        note="amount and date pair unique on both sides",
                    ),
                    MatchEvidence(
                        field="value_date",
                        bank_value=_as_iso(settled_on),
                        settlement_value=_as_iso(settled_on),
                        note="exact date agreement; no window applied at tier 0",
                    ),
                ),
            )
        )
    return matches


def _as_iso(value: object) -> str:
    return value.isoformat() if isinstance(value, date) else str(value)


def match_tier0(
    bank_txns: Sequence[BankRow], settlements: Sequence[SettlementRow]
) -> list[ProposedMatch]:
    """Match by exact reference, then by unique amount-and-date over what is left.

    The reference rule runs first because a shared reference is direct evidence, while
    an amount coincidence is not. Running it first also shrinks the set the weaker rule
    may draw from.
    """
    matches = _match_on_reference(bank_txns, settlements)

    claimed_bank = {match.bank_txn_id for match in matches}
    claimed_settlements = {sid for match in matches for sid in match.settlement_ids}

    residual_bank = [row for row in bank_txns if row.bank_txn_id not in claimed_bank]
    residual_settlements = [
        row for row in settlements if row.settlement_id not in claimed_settlements
    ]

    matches.extend(_match_on_amount_and_date(residual_bank, residual_settlements))
    return matches
