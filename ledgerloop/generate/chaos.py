"""Chaos injectors — the realism layer.

Each injector sits behind an independently toggleable flag so the ablation table can
attribute failures to specific real-world phenomena. See PROJECT_SPEC section 5.5.

The twelve flags split into two classes, and the split is load-bearing:

* **Structural** injectors change which rows exist, so they change ground truth. They
  run *before* truth is frozen.
* **Cosmetic** injectors degrade only how a fact was written down. Truth is already
  frozen by the time they run, so they cannot alter a link — only make it harder to
  find. ``test_cosmetic_injectors_never_alter_ground_truth`` enforces that.

DECOY_SUBSET is the important one: it plants a second subset that sums to the same
credit. A naive matcher picks one and is wrong half the time. LedgerLoop must raise
AMBIGUOUS_SUBSET.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from random import Random

from ledgerloop.generate.fee_model import FeeModel


class ChaosFlag(StrEnum):
    BATCH = "BATCH"
    """N settlements collapse into one bank credit. Tier 2's reason for existing."""

    FEES = "FEES"
    """Apply the real fee model, so net != gross. Strictly this is the domain's
    baseline rather than 'chaos' — with it off, amounts match trivially and the
    fixture becomes easy mode. Named as a flag because section 5.5 lists it."""

    LAG = "LAG"
    """T+N settlement in business days, skipping weekends and holidays."""

    NARRATION_NOISE = "NARRATION_NOISE"
    """UTR prefixed, truncated, case-varied, delimiter-varied. Truncation is bounded
    so the token stays recoverable within Tier 1's Levenshtein budget — deleting the
    reference outright is NO_UTR's job, and conflating the two would make the
    ablation attribute Tier 0 misses to the wrong phenomenon."""

    NO_UTR = "NO_UTR"
    """The reference is gone entirely. Only amount and name survive, so Tier 0
    cannot fire and the record must reach Tier 3."""

    PARTIAL_REFUND = "PARTIAL_REFUND"
    """A refund nets against the same settlement cycle."""

    DUPLICATE_POST = "DUPLICATE_POST"
    """The bank re-posts an identical credit. Caught by the ingest fingerprint."""

    ORPHAN_CREDIT = "ORPHAN_CREDIT"
    """A credit with no gateway counterpart — an out-of-band transfer."""

    OUT_OF_ORDER = "OUT_OF_ORDER"
    """File order does not follow value date."""

    PAISE_DRIFT = "PAISE_DRIFT"
    """Rounding drift of one to three paise."""

    NAME_VARIANT = "NAME_VARIANT"
    """'ACME RETAIL PVT LTD' versus 'Acme Retail Private Limited'."""

    DECOY_SUBSET = "DECOY_SUBSET"
    """A second valid subset sums to the same credit. Must produce AMBIGUOUS_SUBSET."""

    FEE_DRIFT = "FEE_DRIFT"
    """This merchant's real MDR differs from the one we configured.

    The odd one out among the twelve, and deliberately so. Every other injector corrupts
    how a fact was *written down*; this one makes the system's own model of the world
    wrong. Nothing else in §5.5 does that, which is why §8's strongest claim — a wrong fee
    model surfaces as an exception cluster that rule promotion fixes wholesale — had
    nothing to demonstrate itself against until this existed.

    The settlement report and the bank agree with each other. It is Tier 1's
    recomputation that disagrees with both, so correct matches are declined and fall
    through. A promoted FEE_OVERRIDE teaches the real rate and the whole class returns."""


#: Injectors that change which rows exist, and therefore change ground truth.
STRUCTURAL_FLAGS: frozenset[ChaosFlag] = frozenset(
    {
        ChaosFlag.BATCH,
        ChaosFlag.PARTIAL_REFUND,
        ChaosFlag.ORPHAN_CREDIT,
        ChaosFlag.DUPLICATE_POST,
        ChaosFlag.DECOY_SUBSET,
    }
)

#: Injectors that only degrade observability. Truth is frozen before these run.
COSMETIC_FLAGS: frozenset[ChaosFlag] = frozenset(ChaosFlag) - STRUCTURAL_FLAGS


@dataclass(frozen=True)
class ChaosProfile:
    """A named corruption level.

    ``intensity`` is the share of *eligible* rows an enabled injector affects. It is
    deliberately separate from the flag set so a fixture can enable a phenomenon
    without saturating the data with it.
    """

    flags: frozenset[ChaosFlag]
    intensity: float = 0.35

    def enabled(self, flag: ChaosFlag) -> bool:
        return flag in self.flags

    def fires(self, rng: Random, flag: ChaosFlag) -> bool:
        """True when this injector should affect the row drawing from ``rng``.

        ``rng`` must be a per-concern stream. Sharing one sequential stream across
        injectors would make toggling any flag shift every later draw, and the
        ablation table could no longer attribute a difference to anything.
        """
        return self.enabled(flag) and rng.random() < self.intensity


EASY = ChaosProfile(
    flags=frozenset({ChaosFlag.FEES, ChaosFlag.LAG}),
    intensity=0.10,
)
"""Proves the pipeline works at all: fees and timing, everything still 1:1."""

REALISTIC = ChaosProfile(
    flags=EASY.flags
    | {
        ChaosFlag.BATCH,
        ChaosFlag.NARRATION_NOISE,
        ChaosFlag.PAISE_DRIFT,
        ChaosFlag.NAME_VARIANT,
        ChaosFlag.OUT_OF_ORDER,
    },
    intensity=0.35,
)
"""The tuning fixture. Carries no decoys — see ADVERSARIAL."""

ADVERSARIAL = ChaosProfile(flags=frozenset(ChaosFlag), intensity=0.60)
"""Held out. Every injector on, including the decoys the cascade must decline."""

PROFILES: dict[str, ChaosProfile] = {
    "easy": EASY,
    "realistic": REALISTIC,
    "adversarial": ADVERSARIAL,
}

#: A bank-reference-shaped token: four letters then at least six alphanumerics.
UTR_TOKEN = re.compile(r"[A-Z]{4}[A-Z0-9]{6,}")

_NARRATION_PREFIXES = ("NEFT-CR", "IMPS/P2A", "RTGS-CR", "UPI/CR", "NEFT")
_NARRATION_BANKS = ("HDFC", "ICIC", "SBIN", "AXIS", "KKBK")
_NARRATION_BRANCHES = ("BLR", "MUM", "DEL", "HYD", "PNQ")
_SUFFIX_EXPANSIONS = {"PVT": "Private", "LTD": "Limited", "PVT.": "Private", "LTD.": "Limited"}

#: Bounded so the mangled token stays inside Tier 1's Levenshtein budget.
_MAX_TRUNCATION = 2

#: Instrument codes some banks run straight into the reference with no separator. These
#: are deliberately *not* in `tier0.BANK_PREFIXES`: they are the thing the system has to
#: be taught, and a normaliser that already knew them would leave the promotion loop with
#: nothing to learn.
_GLUED_PREFIXES = ("MMTCR", "TRFCR", "CMSCR")

#: Share of noisy narrations that glue the prefix on. Low enough to stay a minority
#: phenomenon, high enough that resolving a handful of exceptions reveals the pattern.
_GLUED_PREFIX_RATE = 0.30

#: How far this merchant's real MDR sits from the configured one, in basis points.
#: A whole percentage point: large enough to fall outside Tier 1's 0.5% band on every
#: settlement rather than only the large ones, so the failure is a clean class rather
#: than a size-dependent smear.
FEE_DRIFT_BPS = 100

#: The customer whose pricing we have wrong. One merchant, not a random scatter — §8's
#: claim is about a *systematic* wrong assumption, and a drift sprinkled at random would
#: be indistinguishable from noise and would generalise to nothing.
FEE_DRIFT_CUSTOMER = "NIMBUS TEXTILES LTD"


def clean_narration(utr: str | None, name: str, branch: str) -> str:
    """The narration a well-behaved bank would write. Chaos degrades this."""
    parts = ["NEFT-CR", "HDFC", utr or "", name, branch]
    return "/".join(part for part in parts if part)


def noisy_narration(rng: Random, utr: str | None, name: str, amount_paise: int) -> str:
    """Prefix, truncate, case-vary and delimiter-vary the reference.

    Truncation is capped at ``_MAX_TRUNCATION`` characters: the point of this
    injector is to defeat Tier 0's exact match while leaving Tier 1's fuzzy match a
    real chance. Removing the reference altogether is a different phenomenon.
    """
    token = utr or ""
    if rng.random() < 0.35 and len(token) > 6:
        token = token[: len(token) - rng.randint(1, _MAX_TRUNCATION)]
    if rng.random() < 0.40:
        token = token.lower()
    if rng.random() < 0.30 and len(token) > 6:
        cut = rng.randrange(3, len(token) - 2)
        token = f"{token[:cut]}-{token[cut:]}"

    # §5.5 lists "UTR prefixed" among the manglings, and this is what that means: the
    # bank's instrument code runs straight into the reference with no separator. Rendering
    # it as its own delimited field — which this did until day 11 — lets the tokeniser
    # split it off for free, so the phenomenon never actually reaches the matcher.
    #
    # It is also the only failure class in the fixtures that a promoted rule can repair,
    # which is what makes §9.3's lift measurable rather than always zero.
    glued = rng.random() < _GLUED_PREFIX_RATE and token
    if glued:
        token = f"{rng.choice(_GLUED_PREFIXES)}{token}"

    parts = [rng.choice(_NARRATION_PREFIXES), rng.choice(_NARRATION_BANKS), token, name]
    if rng.random() < 0.25:
        parts.append(f"INR{amount_paise // 100}")
    parts.append(rng.choice(_NARRATION_BRANCHES))
    return "/".join(part for part in parts if part)


def drop_utr(rng: Random, narration: str) -> str:
    """Remove every reference-shaped token, leaving only amount and name as evidence.

    ``rng`` is unused today but kept in the signature: every injector is called the
    same way, and a future variant of this one will want to choose *how* the
    reference goes missing.
    """
    del rng
    stripped = UTR_TOKEN.sub("", narration)
    collapsed = re.sub(r"[-/]{2,}", "/", stripped)
    return collapsed.strip("-/ ")


def name_variant(rng: Random, name: str) -> str:
    """Rewrite a legal name the way a human or a second system would.

    The leading token always survives, so Tier 1's fuzzy name match has something to
    work with. A variant nobody could recognise is not a variant, it is a different
    customer.
    """
    tokens = name.replace(".", "").split()
    style = rng.choice(("expand", "title", "compact", "expand_title"))

    if style in {"expand", "expand_title"}:
        tokens = [_SUFFIX_EXPANSIONS.get(token.upper(), token) for token in tokens]
    if style in {"title", "expand_title"}:
        tokens = [token.title() for token in tokens]
    if style == "compact":
        tokens = [token for token in tokens if token.upper() not in _SUFFIX_EXPANSIONS]

    return " ".join(tokens)


def paise_drift(rng: Random, amount_paise: int) -> int:
    """Rounding drift of one to three paise, never zero.

    Section 5.5 fixes the band. Anything wider stops being drift and becomes a
    different amount, which Tier 1 should reject rather than absorb.
    """
    return amount_paise + rng.choice((-3, -2, -1, 1, 2, 3))


def lagged_value_date(rng: Random, settled_on: date, fee_model: FeeModel) -> date:
    """Slip the credit by a business day now and then.

    Settlement cycles slip without anything being wrong; Tier 1's window has slack
    for exactly this. All date arithmetic goes through the fee model so weekends and
    holidays are handled in one place.
    """
    return fee_model.business_days_after(settled_on, rng.choice((0, 0, 0, 1)))
