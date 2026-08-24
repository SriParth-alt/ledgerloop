"""Chaos injectors and their independence.

§5.5 says each injector sits behind an independently toggleable flag "so the ablation
table can attribute failures to specific real-world phenomena". That sentence is a
hard constraint on the RNG design, not a description of a config file.

If enabling NARRATION_NOISE shifts the random stream, then every amount and every date
changes too, `easy` and `realistic` share no rows, and a difference between two ablation
runs cannot be attributed to anything. The independence tests below are the ones that
force per-concern RNG streams rather than one sequential stream.
"""

from __future__ import annotations

import re
from random import Random

from ledgerloop.config import DEFAULT_MATCH_CONFIG
from ledgerloop.generate.chaos import (
    ADVERSARIAL,
    EASY,
    REALISTIC,
    ChaosFlag,
    ChaosProfile,
    drop_utr,
    name_variant,
    noisy_narration,
    paise_drift,
)
from ledgerloop.generate.synth import GeneratedBatch, generate_batch

SETTLEMENT_COUNT = 60
STRUCTURAL = frozenset(
    {
        ChaosFlag.BATCH,
        ChaosFlag.PARTIAL_REFUND,
        ChaosFlag.ORPHAN_CREDIT,
        ChaosFlag.DUPLICATE_POST,
        ChaosFlag.DECOY_SUBSET,
    }
)


def _with(*flags: ChaosFlag, intensity: float = 0.5) -> ChaosProfile:
    return ChaosProfile(flags=frozenset(flags), intensity=intensity)


def _batch(profile: ChaosProfile) -> GeneratedBatch:
    return generate_batch(settlements=SETTLEMENT_COUNT, seed=42, profile=profile)


# --- flag independence ----------------------------------------------------------


def test_enabling_narration_noise_changes_only_narrations() -> None:
    """The single most important property in this module.

    A cosmetic injector degrades how a fact is *written down*. It must not change the
    fact. If this fails, the whole ablation table is comparing unrelated datasets.
    """
    base = _batch(_with(ChaosFlag.FEES, ChaosFlag.BATCH))
    noisy = _batch(_with(ChaosFlag.FEES, ChaosFlag.BATCH, ChaosFlag.NARRATION_NOISE))

    assert [row.net_amount_paise for row in base.settlements] == [
        row.net_amount_paise for row in noisy.settlements
    ]
    assert [row.settled_on for row in base.settlements] == [
        row.settled_on for row in noisy.settlements
    ]
    assert [row.credit_paise for row in base.bank_txns] == [
        row.credit_paise for row in noisy.bank_txns
    ]
    assert [row.narration for row in base.bank_txns] != [
        row.narration for row in noisy.bank_txns
    ]


def test_enabling_paise_drift_changes_only_credited_amounts() -> None:
    base = _batch(_with(ChaosFlag.FEES))
    drifted = _batch(_with(ChaosFlag.FEES, ChaosFlag.PAISE_DRIFT))

    assert [row.bank_txn_id for row in base.bank_txns] == [
        row.bank_txn_id for row in drifted.bank_txns
    ]
    assert [row.value_date for row in base.bank_txns] == [
        row.value_date for row in drifted.bank_txns
    ]
    assert [row.credit_paise for row in base.bank_txns] != [
        row.credit_paise for row in drifted.bank_txns
    ]


def test_cosmetic_injectors_never_alter_ground_truth() -> None:
    """Truth is frozen before cosmetic chaos runs. Adding corruption may make a link
    harder to *find*; it must never make it a different link."""
    structural = _with(ChaosFlag.FEES, ChaosFlag.BATCH, ChaosFlag.ORPHAN_CREDIT)
    cosmetic = ChaosProfile(
        flags=structural.flags
        | {
            ChaosFlag.NARRATION_NOISE,
            ChaosFlag.NO_UTR,
            ChaosFlag.NAME_VARIANT,
            ChaosFlag.OUT_OF_ORDER,
        },
        intensity=structural.intensity,
    )

    def fingerprint(batch: GeneratedBatch) -> list[tuple[str, str | None, str | None, str]]:
        return sorted(
            (link.bank_txn_id, link.settlement_id, link.invoice_id, str(link.link_type))
            for link in batch.links
        )

    assert fingerprint(_batch(structural)) == fingerprint(_batch(cosmetic))


# --- individual injectors -------------------------------------------------------


def test_paise_drift_stays_within_the_documented_band() -> None:
    """§5.5 says ±1-3 paise. Wider than that stops being rounding drift and starts
    being a different amount, which Tier 1 should reject rather than absorb."""
    rng = Random(7)
    for _ in range(200):
        original = 5_000_00
        drifted = paise_drift(rng, original)
        assert type(drifted) is int
        assert 1 <= abs(drifted - original) <= 3


def test_drop_utr_removes_every_utr_token() -> None:
    """§5.5 NO_UTR: only amount and name survive, so Tier 0 cannot possibly fire."""
    narration = drop_utr(Random(7), "NEFT-CR/HDFC/RZRPY0034821/ACME RETAIL PVT/BLR")

    assert "RZRPY0034821" not in narration
    assert not re.search(r"[A-Z]{4}[A-Z0-9]{6,}", narration)


def test_noisy_narration_keeps_the_utr_within_tier1_fuzzy_distance() -> None:
    """NARRATION_NOISE mangles the reference; it must not delete it. Deleting it is
    NO_UTR's job, and conflating the two would make the ablation table attribute
    Tier 0 misses to the wrong phenomenon.

    §5.5 lists "truncated" among the manglings, so the token legitimately loses
    characters. The binding constraint is Tier 1's fuzzy budget: whatever survives
    must stay within `fuzzy_utr_max_distance` of the original, or the record is
    unreachable by any tier below 3 and the injector has silently become NO_UTR.
    """
    utr = "RZRPY0034821"
    budget = DEFAULT_MATCH_CONFIG.fuzzy_utr_max_distance
    prefixes = [utr[: len(utr) - dropped] for dropped in range(budget + 1)]
    truncation_seen = False

    for seed in range(40):
        narration = noisy_narration(Random(seed), utr, "ACME RETAIL PVT LTD", 4_82_200)
        normalised = re.sub(r"[^A-Z0-9]", "", narration.upper())
        assert any(prefix in normalised for prefix in prefixes), narration
        if utr not in normalised:
            truncation_seen = True

    assert truncation_seen, "no truncation in 40 draws — Tier 1's fuzzy path is untested"


def test_name_variant_rewrites_the_name_without_destroying_it() -> None:
    """§5.5 NAME_VARIANT: 'ACME RETAIL PVT LTD' vs 'Acme Retail Private Limited'.
    Tier 1 fuzzy matching must still have something to work with."""
    original = "ACME RETAIL PVT LTD"
    variants = {name_variant(Random(seed), original) for seed in range(20)}

    assert any(variant != original for variant in variants)
    for variant in variants:
        assert "ACME" in variant.upper()


# --- profiles -------------------------------------------------------------------


def test_easy_profile_contains_no_structural_chaos() -> None:
    """`easy` exists to prove the pipeline works at all. Cardinality problems and
    ambiguity belong to the harder fixtures."""
    assert EASY.flags & STRUCTURAL == frozenset()


def test_adversarial_profile_enables_every_injector() -> None:
    assert ADVERSARIAL.flags == frozenset(ChaosFlag)


def test_adversarial_is_the_only_profile_planting_decoys() -> None:
    """The held-out fixture carries the trap. Tuning happens on `realistic`, so a
    decoy there would let the cascade be fitted to the thing it must decline."""
    assert ChaosFlag.DECOY_SUBSET not in EASY.flags
    assert ChaosFlag.DECOY_SUBSET not in REALISTIC.flags
    assert ChaosFlag.DECOY_SUBSET in ADVERSARIAL.flags


def test_profile_reports_enabled_flags() -> None:
    profile = _with(ChaosFlag.BATCH)

    assert profile.enabled(ChaosFlag.BATCH)
    assert not profile.enabled(ChaosFlag.DECOY_SUBSET)
