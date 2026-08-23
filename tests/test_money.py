"""Money arithmetic. Everything else in the system rests on these being right."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgerloop.money import (
    apply_rate,
    paise_to_rupees,
    rupees_to_paise,
    within_tolerance,
)


class TestParsing:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("1234.56", 123_456),
            ("1234.5", 123_450),
            ("1234", 123_400),
            ("0.01", 1),
            ("0.00", 0),
            ("1,234.56", 123_456),
            ("\u20b91234.56", 123_456),
            ("  1234.56  ", 123_456),
            ("-1234.56", -123_456),
        ],
    )
    def test_parses_rupee_strings_to_paise(self, text: str, expected: int) -> None:
        assert rupees_to_paise(text) == expected

    @pytest.mark.parametrize("bad", ["", "abc", "12.345", "1.2.3", "12."])
    def test_rejects_ambiguous_amounts_rather_than_guessing(self, bad: str) -> None:
        with pytest.raises(ValueError):
            rupees_to_paise(bad)

    @given(st.integers(min_value=-10**12, max_value=10**12))
    def test_roundtrip_is_lossless(self, paise: int) -> None:
        assert rupees_to_paise(paise_to_rupees(paise)) == paise


class TestRateApplication:
    def test_two_percent_of_one_lakh(self) -> None:
        assert apply_rate(10_000_000, 200) == 200_000

    def test_rounds_half_away_from_zero_not_bankers(self) -> None:
        # 1 paise at 5000bps = 0.5 paise, must round up to 1, not down to 0.
        assert apply_rate(1, 5_000) == 1

    def test_zero_rate_is_zero(self) -> None:
        assert apply_rate(123_456, 0) == 0

    def test_rejects_negative_rate(self) -> None:
        with pytest.raises(ValueError):
            apply_rate(100, -1)

    @given(
        base=st.integers(min_value=0, max_value=10**11),
        rate=st.integers(min_value=0, max_value=10_000),
    )
    def test_result_never_exceeds_base_for_sub_100_percent_rates(
        self, base: int, rate: int
    ) -> None:
        assert 0 <= apply_rate(base, rate) <= base + 1


class TestTolerance:
    def test_flat_band_applies_to_small_amounts(self) -> None:
        assert within_tolerance(10_000, 10_050, abs_paise=100, rel_bps=50)

    def test_relative_band_applies_to_large_amounts(self) -> None:
        # 0.5% of 10 lakh is far wider than the 1 rupee flat band.
        assert within_tolerance(100_000_000, 100_400_000, abs_paise=100, rel_bps=50)

    def test_rejects_beyond_both_bands(self) -> None:
        assert not within_tolerance(10_000, 20_000, abs_paise=100, rel_bps=50)

    @given(amount=st.integers(min_value=1, max_value=10**10))
    def test_is_reflexive(self, amount: int) -> None:
        assert within_tolerance(amount, amount, abs_paise=0, rel_bps=0)
