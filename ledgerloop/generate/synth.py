"""Seeded synthetic data generator.

Emits ``ledger_invoices.csv``, ``pg_settlements.csv``, ``bank_statement.csv`` and
``truth_links.csv`` from a seed. ``--seed 42`` reproduces byte-identical files, so
every ``random`` draw comes from an explicit stream rather than module-level state.

**Truth first, then render, then corrupt.** The generator builds the world — invoices,
settlements, bank credits, with links known by construction — then renders each source
file as a lossy view of that world, then degrades the views. Ground truth is never
reverse-engineered from the output. If it were, a generator bug and a matcher bug could
cancel and the metrics would look excellent.

**Per-concern RNG streams.** Every decision draws from ``_stream(seed, concern, index)``
rather than one sequential generator. Section 5.5 requires flags to be independently
toggleable so the ablation can attribute failures; with a single stream, enabling any
injector shifts every later draw and two runs share no rows at all. The hash is
``blake2b`` and never ``hash()``, which is salted per process and would silently break
byte-identical reproduction between runs.

Ship three fixtures: easy, realistic, adversarial. Tune the cascade only against
``realistic``; hold ``adversarial`` out so the final numbers are not self-graded.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path
from random import Random

from ledgerloop.generate.chaos import (
    PROFILES,
    ChaosFlag,
    ChaosProfile,
    clean_narration,
    drop_utr,
    lagged_value_date,
    name_variant,
    noisy_narration,
    paise_drift,
)
from ledgerloop.generate.fee_model import SETTLEMENT_FEE_MODEL, FeeModel, PaymentMethod
from ledgerloop.ingest.schemas import BankRow, InvoiceRow, SettlementRow

INVOICES_FILE = "ledger_invoices.csv"
SETTLEMENTS_FILE = "pg_settlements.csv"
BANK_FILE = "bank_statement.csv"
TRUTH_FILE = "truth_links.csv"

DEFAULT_START = date(2026, 8, 3)
OPENING_BALANCE_PAISE = 25_00_00_000

#: Share of settlements touched by a refund or a dispute.
#:
#: Deliberately independent of ``ChaosProfile.intensity``. Intensity governs how badly
#: the *observability* of a batch is degraded — noisy narrations, missing references,
#: drifting paise. It has no business governing how often money fails to arrive. Real
#: refund and dispute rates sit in low single digits, and a statement where a large
#: share of payments never landed stops looking like a bank statement long before
#: anyone checks the metrics computed from it.
REFUND_EVENT_RATE = 0.05

#: Outcome mix once a refund event fires. Only ``partial_refund`` still settles; the
#: other two produce no bank credit and therefore no truth link, which is correct —
#: nothing is wrong with them and no matcher should be penalised for not finding one.
_REFUND_OUTCOMES = ("partial_refund", "partial_refund", "refunded", "disputed")

_MERCHANT_ID = "MERCH001"
_CUSTOMER_NAMES = (
    "ACME RETAIL PVT LTD",
    "NIMBUS TEXTILES LTD",
    "SARASWATI TRADERS",
    "BLUEPEAK LOGISTICS PVT LTD",
    "KAVERI AGRO EXPORTS",
    "ORBIT DIGITAL PVT LTD",
    "MAHALAXMI STORES",
    "VERTEX HEALTHCARE LTD",
    "SUNRISE PACKAGING PVT LTD",
    "TRIDENT MOTORS LTD",
)
_CREDITABLE_STATUSES = frozenset({"captured", "partial_refund"})


class LinkType(StrEnum):
    """Ground-truth link vocabulary — **cardinality only**.

    What *happened* to a link (a refund netted against it, the bank re-posting it) is
    recorded in ``TruthLink.chaos_tags``, never here. The two are independent
    questions, and conflating them silently loses batch membership for every refunded
    row — which is exactly the attribution ``eval/`` needs on day 8.

    Fixed here because ``eval/`` consumes it; changing it later means regenerating
    every published fixture.
    """

    ONE_TO_ONE = "one_to_one"
    """One settlement explains one credit."""

    BATCH_MEMBER = "batch_member"
    """One of N settlements collapsed into a single credit. Tier 2's problem."""

    ORPHAN_CREDIT = "orphan_credit"
    """A credit with no settlement behind it at all."""


@dataclass(frozen=True)
class TruthLink:
    """One row of ``truth_links.csv``. Never seen by the matcher."""

    bank_txn_id: str
    settlement_id: str | None
    invoice_id: str | None
    link_type: LinkType
    chaos_tags: tuple[ChaosFlag, ...] = ()


@dataclass(frozen=True)
class GeneratedBatch:
    """The three source views plus the ground truth that explains them."""

    invoices: list[InvoiceRow]
    settlements: list[SettlementRow]
    bank_txns: list[BankRow]
    links: list[TruthLink]


@dataclass
class _Working:
    """Mutable scratch record. Converted to a frozen ``SettlementRow`` at the end."""

    index: int
    invoice_id: str
    settlement_id: str
    customer_id: str
    customer_name: str
    method: PaymentMethod
    gross_paise: int
    captured_at: date
    settled_on: date
    utr: str | None
    status: str
    refund_paise: int = 0
    tags: set[ChaosFlag] = field(default_factory=set)


def _stream(seed: int, concern: str, index: int = 0) -> Random:
    """A random stream scoped to one concern and one row.

    Keying by concern is what makes chaos flags independent: toggling
    NARRATION_NOISE draws from the ``narration`` stream and disturbs nothing else.
    """
    digest = hashlib.blake2b(f"{seed}:{concern}:{index}".encode(), digest_size=8).digest()
    return Random(int.from_bytes(digest, "big"))


def _capture_window(settlements: int) -> int:
    """Business days over which captures are spread.

    Deliberately narrow: settlements have to share dates for batching to occur at
    all, and a batch is what Tier 2 exists to unpick.
    """
    return max(3, min(20, settlements // 6))


def _fee_components(
    working: _Working, profile: ChaosProfile, fee_model: FeeModel
) -> dict[str, int]:
    if not profile.enabled(ChaosFlag.FEES):
        return {
            "gross_paise": working.gross_paise,
            "fee_paise": 0,
            "gst_on_fee_paise": 0,
            "tds_paise": 0,
            "net_paise": working.gross_paise,
        }
    return fee_model.breakdown(working.gross_paise, working.method)


def _build_settlements(
    *, count: int, seed: int, profile: ChaosProfile, fee_model: FeeModel, start: date
) -> list[_Working]:
    window = _capture_window(count)
    rows: list[_Working] = []

    for index in range(count):
        rng = _stream(seed, "settlement", index)
        name = _CUSTOMER_NAMES[rng.randrange(len(_CUSTOMER_NAMES))]
        method = list(PaymentMethod)[rng.randrange(len(PaymentMethod))]
        gross = rng.randrange(500_00, 50_000_00)
        captured_at = fee_model.business_days_after(start, rng.randrange(window))
        settled_on = (
            fee_model.settlement_date(captured_at)
            if profile.enabled(ChaosFlag.LAG)
            else captured_at
        )

        status = "captured"
        refund = 0
        refund_rng = _stream(seed, "refund", index)
        if profile.enabled(ChaosFlag.PARTIAL_REFUND) and refund_rng.random() < REFUND_EVENT_RATE:
            status = refund_rng.choice(_REFUND_OUTCOMES)
            if status == "partial_refund":
                refund = refund_rng.randrange(100_00, max(200_00, gross // 4))

        utr_rng = _stream(seed, "utr", index)
        rows.append(
            _Working(
                index=index,
                invoice_id=f"INV{index + 1:05d}",
                settlement_id=f"STL{index + 1:05d}",
                customer_id=f"CUST{_CUSTOMER_NAMES.index(name) + 1:03d}",
                customer_name=name,
                method=method,
                gross_paise=gross,
                captured_at=captured_at,
                settled_on=settled_on,
                utr=f"RZRPY{utr_rng.randrange(1_000_000, 9_999_999):07d}",
                status=status,
                refund_paise=refund,
            )
        )
    return rows


def _plant_decoy(rows: list[_Working], seed: int) -> list[tuple[_Working, _Working]]:
    """Make a second subset sum to the same credit, exactly.

    Two different customers paying identical amounts on the same day is an ordinary
    occurrence, and it is the cheapest way to produce ambiguity that is genuinely
    arithmetically perfect — which ADR-003 requires, because a decoy that only
    *nearly* collides would be resolvable by a tiebreak and prove nothing.

    Returns the two pinned pairs so grouping keeps them as separate credits.
    """
    eligible = [row for row in rows if row.status in _CREDITABLE_STATUSES]
    if len(eligible) < 4:
        return []

    by_date: dict[date, list[_Working]] = {}
    for row in eligible:
        by_date.setdefault(row.settled_on, []).append(row)

    chosen = next((group for group in by_date.values() if len(group) >= 4), None)
    if chosen is None:
        # Force it. A fixture that silently ships without its centrepiece is worse
        # than one that rearranges four rows to guarantee it.
        chosen = eligible[:4]
        target_date = chosen[0].settled_on
        for row in chosen:
            row.settled_on = target_date

    rng = _stream(seed, "decoy", 0)
    picked = chosen[:4]
    rng.shuffle(picked)
    original, mirror = (picked[0], picked[1]), (picked[2], picked[3])

    for source, target in zip(original, mirror, strict=True):
        target.gross_paise = source.gross_paise
        target.method = source.method
        target.status = source.status
        target.refund_paise = source.refund_paise

    for row in (*original, *mirror):
        row.tags.add(ChaosFlag.DECOY_SUBSET)

    return [original, mirror]


def _group_into_credits(
    rows: list[_Working], *, seed: int, profile: ChaosProfile, pinned: list[tuple[_Working, ...]]
) -> list[list[_Working]]:
    pinned_ids = {row.settlement_id for group in pinned for row in group}
    groups: list[list[_Working]] = [list(group) for group in pinned]

    remaining = [
        row
        for row in rows
        if row.status in _CREDITABLE_STATUSES and row.settlement_id not in pinned_ids
    ]
    if not profile.enabled(ChaosFlag.BATCH):
        groups.extend([row] for row in remaining)
        return groups

    by_date: dict[date, list[_Working]] = {}
    for row in remaining:
        by_date.setdefault(row.settled_on, []).append(row)

    for settled_on in sorted(by_date):
        members = by_date[settled_on]
        rng = _stream(seed, "batch", settled_on.toordinal())
        cursor = 0
        while cursor < len(members):
            size = rng.choice((1, 2, 2, 3)) if profile.fires(rng, ChaosFlag.BATCH) else 1
            groups.append(members[cursor : cursor + size])
            cursor += size

    return groups


def _net_of(working: _Working, profile: ChaosProfile, fee_model: FeeModel) -> int:
    components = _fee_components(working, profile, fee_model)
    return components["net_paise"] - working.refund_paise


def generate_batch(
    *,
    settlements: int,
    seed: int,
    profile: ChaosProfile,
    fee_model: FeeModel = SETTLEMENT_FEE_MODEL,
    start: date = DEFAULT_START,
) -> GeneratedBatch:
    """Build one complete batch: three source views plus the truth that explains them.

    ``settlements`` counts settlements, the entity actually being reconciled. Invoice
    count matches it; bank rows are fewer whenever BATCH collapses several into one.
    """
    working = _build_settlements(
        count=settlements, seed=seed, profile=profile, fee_model=fee_model, start=start
    )

    pinned: list[tuple[_Working, ...]] = []
    if profile.enabled(ChaosFlag.DECOY_SUBSET):
        pinned = list(_plant_decoy(working, seed))

    groups = _group_into_credits(working, seed=seed, profile=profile, pinned=pinned)

    invoice_rows = [_to_invoice_row(row) for row in working]
    settlement_rows = [_to_settlement_row(row, profile, fee_model) for row in working]

    bank_rows, links = _build_bank_side(
        groups, seed=seed, profile=profile, fee_model=fee_model, settlement_count=settlements
    )

    return GeneratedBatch(
        invoices=invoice_rows,
        settlements=settlement_rows,
        bank_txns=bank_rows,
        links=links,
    )


def _to_invoice_row(working: _Working) -> InvoiceRow:
    return InvoiceRow(
        invoice_id=working.invoice_id,
        merchant_id=_MERCHANT_ID,
        customer_id=working.customer_id,
        customer_name=working.customer_name,
        invoice_amount_paise=working.gross_paise,
        currency="INR",
        issue_date=working.captured_at,
        due_date=working.captured_at,
        status="raised" if working.status == "captured" else working.status,
    )


def _to_settlement_row(
    working: _Working, profile: ChaosProfile, fee_model: FeeModel
) -> SettlementRow:
    components = _fee_components(working, profile, fee_model)
    return SettlementRow(
        settlement_id=working.settlement_id,
        payment_id=f"PAY{working.index + 1:05d}",
        order_id=f"ORD{working.index + 1:05d}",
        invoice_ref=working.invoice_id,
        customer_name=working.customer_name,
        method=working.method,
        gross_amount_paise=components["gross_paise"],
        fee_paise=components["fee_paise"],
        gst_on_fee_paise=components["gst_on_fee_paise"],
        tds_paise=components["tds_paise"],
        net_amount_paise=components["net_paise"] - working.refund_paise,
        captured_at=working.captured_at,
        settled_on=working.settled_on,
        utr=working.utr,
        status=working.status,
    )


def _narration_for(
    members: list[_Working], *, seed: int, profile: ChaosProfile, index: int
) -> str:
    lead = members[0]
    name_rng = _stream(seed, "name", index)
    name = (
        name_variant(name_rng, lead.customer_name)
        if profile.fires(name_rng, ChaosFlag.NAME_VARIANT)
        else lead.customer_name
    )

    narration_rng = _stream(seed, "narration", index)
    branch = ("BLR", "MUM", "DEL", "HYD", "PNQ")[index % 5]
    baseline = clean_narration(lead.utr, name, branch)

    if profile.fires(_stream(seed, "no_utr", index), ChaosFlag.NO_UTR):
        return drop_utr(narration_rng, baseline)
    if profile.fires(narration_rng, ChaosFlag.NARRATION_NOISE):
        return noisy_narration(narration_rng, lead.utr, name, lead.gross_paise)
    return baseline


def _build_bank_side(
    groups: list[list[_Working]],
    *,
    seed: int,
    profile: ChaosProfile,
    fee_model: FeeModel,
    settlement_count: int,
) -> tuple[list[BankRow], list[TruthLink]]:
    """Render bank credits from frozen truth, then degrade the rendering.

    Everything below this line is cosmetic: it may make a link harder to find, but
    the links themselves are already decided by ``groups``.
    """
    pending: list[tuple[str, date, int, str, list[_Working]]] = []
    links: list[TruthLink] = []

    ordered = sorted(groups, key=lambda group: (group[0].settled_on, group[0].settlement_id))

    for index, members in enumerate(ordered):
        txn_id = f"BNK{index + 1:05d}"
        settled_on = members[0].settled_on
        total = sum(_net_of(row, profile, fee_model) for row in members)

        is_decoy = any(ChaosFlag.DECOY_SUBSET in row.tags for row in members)
        drift_rng = _stream(seed, "drift", index)
        # A decoy must stay arithmetically perfect: drift would leave both
        # explanations merely approximate, and ambiguity would become a tiebreak.
        if not is_decoy and profile.fires(drift_rng, ChaosFlag.PAISE_DRIFT):
            total = paise_drift(drift_rng, total)

        value_date = settled_on
        if profile.enabled(ChaosFlag.LAG):
            value_date = lagged_value_date(_stream(seed, "lag", index), settled_on, fee_model)

        narration = _narration_for(members, seed=seed, profile=profile, index=index)
        pending.append((txn_id, value_date, total, narration, members))

        link_type = LinkType.BATCH_MEMBER if len(members) > 1 else LinkType.ONE_TO_ONE
        for row in members:
            # Cardinality goes in link_type; what happened to the row goes in tags.
            tags = set(row.tags)
            if row.status == "partial_refund":
                tags.add(ChaosFlag.PARTIAL_REFUND)
            links.append(
                TruthLink(
                    bank_txn_id=txn_id,
                    settlement_id=row.settlement_id,
                    invoice_id=row.invoice_id,
                    link_type=link_type,
                    chaos_tags=tuple(sorted(tags)),
                )
            )

    pending, links = _add_orphans(
        pending, links, seed=seed, profile=profile, settlement_count=settlement_count
    )
    pending, links = _add_duplicate_post(pending, links, seed=seed, profile=profile)

    if profile.enabled(ChaosFlag.OUT_OF_ORDER):
        _stream(seed, "order", 0).shuffle(pending)

    bank_rows: list[BankRow] = []
    balance = OPENING_BALANCE_PAISE
    for txn_id, value_date, credit, narration, _members in pending:
        balance += credit
        bank_rows.append(
            BankRow(
                bank_txn_id=txn_id,
                value_date=value_date,
                narration=narration,
                credit_paise=credit,
                debit_paise=0,
                balance_paise=balance,
            )
        )
    return bank_rows, links


def _add_orphans(
    pending: list[tuple[str, date, int, str, list[_Working]]],
    links: list[TruthLink],
    *,
    seed: int,
    profile: ChaosProfile,
    settlement_count: int,
) -> tuple[list[tuple[str, date, int, str, list[_Working]]], list[TruthLink]]:
    """Credits with no gateway counterpart: out-of-band transfers, interest, refunds."""
    if not profile.enabled(ChaosFlag.ORPHAN_CREDIT) or not pending:
        return pending, links

    rng = _stream(seed, "orphan", 0)
    count = max(1, round(settlement_count * profile.intensity * 0.05))
    for offset in range(count):
        txn_id = f"BNKORPH{offset + 1:03d}"
        template = pending[rng.randrange(len(pending))]
        amount = rng.randrange(1_000_00, 20_000_00)
        narration = f"NEFT-CR/{rng.choice(('HDFC', 'ICIC', 'SBIN'))}/DIRECT TRANSFER/BLR"
        pending.append((txn_id, template[1], amount, narration, []))
        links.append(
            TruthLink(
                bank_txn_id=txn_id,
                settlement_id=None,
                invoice_id=None,
                link_type=LinkType.ORPHAN_CREDIT,
                chaos_tags=(ChaosFlag.ORPHAN_CREDIT,),
            )
        )
    return pending, links


def _add_duplicate_post(
    pending: list[tuple[str, date, int, str, list[_Working]]],
    links: list[TruthLink],
    *,
    seed: int,
    profile: ChaosProfile,
) -> tuple[list[tuple[str, date, int, str, list[_Working]]], list[TruthLink]]:
    """The bank re-posts a credit it has already sent.

    The re-post carries the same money — identical amount, value date and narration —
    under a **new transaction id**. That is deliberate and it is what `ingest/loader.py`
    requires: a byte-identical row would be absorbed by the row fingerprint silently,
    which is exactly the double-counting the loader must surface as
    ``DUPLICATE_SUSPECTED``. It would also collide on ``PRIMARY KEY (run_id,
    bank_txn_id)`` and fail the insert outright.

    Ground truth gives the re-post a link of its own with no settlement behind it.
    Its cardinality really is zero, which is what ``ORPHAN_CREDIT`` means; the reason
    it is zero — a re-post rather than an out-of-band transfer — is an event, and
    ADR-010 puts events in ``chaos_tags``. ``eval/`` tells the two apart by the tag,
    because they carry different expected outcomes: ``ORPHAN_CREDIT`` for one,
    ``DUPLICATE_SUSPECTED`` for the other.
    """
    if not profile.enabled(ChaosFlag.DUPLICATE_POST) or not pending:
        return pending, links

    rng = _stream(seed, "duplicate", 0)
    original_id, value_date, credit, narration, _members = pending[rng.randrange(len(pending))]
    repost_id = "BNKDUP001"
    pending.append((repost_id, value_date, credit, narration, []))

    tagged = [
        TruthLink(
            bank_txn_id=link.bank_txn_id,
            settlement_id=link.settlement_id,
            invoice_id=link.invoice_id,
            link_type=link.link_type,
            chaos_tags=tuple(sorted({*link.chaos_tags, ChaosFlag.DUPLICATE_POST})),
        )
        if link.bank_txn_id == original_id
        else link
        for link in links
    ]
    tagged.append(
        TruthLink(
            bank_txn_id=repost_id,
            settlement_id=None,
            invoice_id=None,
            link_type=LinkType.ORPHAN_CREDIT,
            chaos_tags=(ChaosFlag.DUPLICATE_POST,),
        )
    )
    return pending, tagged


def _write_csv(path: Path, header: list[str], rows: list[list[str]]) -> None:
    """Write UTF-8 CSV with an explicit LF terminator.

    ``newline=""`` plus ``lineterminator="\\n"`` is what keeps output byte-identical
    between a Windows workstation and a Linux CI runner. Without both, the default
    CRLF makes reproducibility true locally and false in CI.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)


def write_batch(batch: GeneratedBatch, out_dir: Path) -> dict[str, Path]:
    """Write all four files and return their paths, keyed by role.

    Callers iterate the mapping rather than naming the ground-truth file, which is
    what keeps ``cli.py`` clear of the tokens ``tests/test_no_truth_leak.py`` forbids
    outside this package.
    """
    out_dir = Path(out_dir)
    paths = {
        "invoices": out_dir / INVOICES_FILE,
        "settlements": out_dir / SETTLEMENTS_FILE,
        "bank_statement": out_dir / BANK_FILE,
        "truth": out_dir / TRUTH_FILE,
    }

    _write_csv(
        paths["invoices"],
        [
            "invoice_id",
            "merchant_id",
            "customer_id",
            "customer_name",
            "invoice_amount_paise",
            "currency",
            "issue_date",
            "due_date",
            "status",
        ],
        [
            [
                row.invoice_id,
                row.merchant_id,
                row.customer_id,
                row.customer_name,
                str(row.invoice_amount_paise),
                row.currency,
                row.issue_date.isoformat(),
                row.due_date.isoformat(),
                row.status,
            ]
            for row in batch.invoices
        ],
    )

    _write_csv(
        paths["settlements"],
        [
            "settlement_id",
            "payment_id",
            "order_id",
            "invoice_ref",
            "customer_name",
            "method",
            "gross_amount_paise",
            "fee_paise",
            "gst_on_fee_paise",
            "tds_paise",
            "net_amount_paise",
            "captured_at",
            "settled_on",
            "utr",
            "status",
        ],
        [
            [
                row.settlement_id,
                row.payment_id,
                row.order_id,
                row.invoice_ref or "",
                row.customer_name,
                str(row.method),
                str(row.gross_amount_paise),
                str(row.fee_paise),
                str(row.gst_on_fee_paise),
                str(row.tds_paise),
                str(row.net_amount_paise),
                row.captured_at.isoformat(),
                row.settled_on.isoformat(),
                row.utr or "",
                row.status,
            ]
            for row in batch.settlements
        ],
    )

    _write_csv(
        paths["bank_statement"],
        ["bank_txn_id", "value_date", "narration", "credit_paise", "debit_paise", "balance_paise"],
        [
            [
                row.bank_txn_id,
                row.value_date.isoformat(),
                row.narration,
                str(row.credit_paise),
                str(row.debit_paise),
                str(row.balance_paise),
            ]
            for row in batch.bank_txns
        ],
    )

    _write_csv(
        paths["truth"],
        ["bank_txn_id", "settlement_id", "invoice_id", "link_type", "chaos_tags"],
        [
            [
                link.bank_txn_id,
                link.settlement_id or "",
                link.invoice_id or "",
                str(link.link_type),
                "|".join(str(tag) for tag in link.chaos_tags),
            ]
            for link in batch.links
        ],
    )

    return paths


def generate_fixture(
    *, fixture: str, settlements: int, seed: int, out_dir: Path
) -> dict[str, Path]:
    """Generate one named fixture and write it to disk.

    An unknown name raises ``KeyError`` rather than falling back to a default: a typo
    that silently produced a different corruption level would invalidate every number
    measured against it.
    """
    profile = PROFILES[fixture]
    batch = generate_batch(settlements=settlements, seed=seed, profile=profile)
    return write_batch(batch, Path(out_dir) / fixture)
