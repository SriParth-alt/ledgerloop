"""Fee model.

These tests matter more than they look. The generator and Tier 1 both call this
module; if it is wrong, the matcher will still appear to work on synthetic data
because both sides share the same error. The property tests below are the guard
against that class of self-consistent wrongness.
"""

from __future__ import annotations

from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from ledgerloop.generate.fee_model import DEFAULT_FEE_MODEL, FeeModel, PaymentMethod


class TestFeeComponents:
    def test_upi_has_no_mdr(self) -> None:
        assert DEFAULT_FEE_MODEL.mdr_fee_paise(10_000_000, PaymentMethod.UPI) == 0

    def test_credit_card_charges_two_percent(self) -> None:
        assert DEFAULT_FEE_MODEL.mdr_fee_paise(10_000_000, PaymentMethod.CREDIT_CARD) == 200_000

    def test_netbanking_charges_a_flat_fee(self) -> None:
        assert DEFAULT_FEE_MODEL.mdr_fee_paise(10_000_000, PaymentMethod.NETBANKING) == 1_800

    def test_debit_card_percentage_is_capped(self) -> None:
        # 0.4% of 10 crore would be 40k rupees; the cap holds it to 1000 rupees.
        fee = DEFAULT_FEE_MODEL.mdr_fee_paise(1_000_000_000, PaymentMethod.DEBIT_CARD)
        assert fee == 100_000

    def test_gst_is_charged_on_the_fee_not_the_gross(self) -> None:
        fee = 200_000
        assert DEFAULT_FEE_MODEL.gst_on_fee_paise(fee) == 36_000


class TestNetAmount:
    def test_breakdown_components_sum_to_net(self) -> None:
        b = DEFAULT_FEE_MODEL.breakdown(10_000_000, PaymentMethod.CREDIT_CARD)
        assert (
            b["gross_paise"] - b["fee_paise"] - b["gst_on_fee_paise"] - b["tds_paise"]
            == b["net_paise"]
        )

    def test_net_is_less_than_gross_when_fees_apply(self) -> None:
        gross = 10_000_000
        assert DEFAULT_FEE_MODEL.net_paise(gross, PaymentMethod.CREDIT_CARD) < gross

    @given(
        gross=st.integers(min_value=100, max_value=10**10),
        method=st.sampled_from(list(PaymentMethod)),
    )
    def test_breakdown_always_reconciles(self, gross: int, method: PaymentMethod) -> None:
        """The property that makes Tier 1 possible at all.

        If gross minus every component does not exactly equal net for every input,
        then the tolerance band in Tier 1 is silently absorbing a bug in this module.
        """
        b = DEFAULT_FEE_MODEL.breakdown(gross, method)
        assert (
            b["gross_paise"] - b["fee_paise"] - b["gst_on_fee_paise"] - b["tds_paise"]
            == b["net_paise"]
        )

    @given(
        gross=st.integers(min_value=100, max_value=10**10),
        method=st.sampled_from(list(PaymentMethod)),
    )
    def test_net_equals_breakdown_net(self, gross: int, method: PaymentMethod) -> None:
        assert (
            DEFAULT_FEE_MODEL.net_paise(gross, method)
            == DEFAULT_FEE_MODEL.breakdown(gross, method)["net_paise"]
        )


class TestSettlementTiming:
    def test_skips_weekend(self) -> None:
        friday = date(2026, 8, 21)
        assert DEFAULT_FEE_MODEL.business_days_after(friday, 1) == date(2026, 8, 24)

    def test_t_plus_two_from_thursday_lands_on_monday(self) -> None:
        thursday = date(2026, 8, 20)
        assert DEFAULT_FEE_MODEL.settlement_date(thursday) == date(2026, 8, 24)

    def test_skips_configured_holiday(self) -> None:
        model = FeeModel(holidays=frozenset({date(2026, 8, 24)}))
        friday = date(2026, 8, 21)
        assert model.business_days_after(friday, 1) == date(2026, 8, 25)

    def test_zero_days_is_identity(self) -> None:
        day = date(2026, 8, 24)
        assert DEFAULT_FEE_MODEL.business_days_after(day, 0) == day

    def test_rejects_negative_days(self) -> None:
        with pytest.raises(ValueError):
            DEFAULT_FEE_MODEL.business_days_after(date(2026, 8, 24), -1)

    def test_window_start_is_not_after_end(self) -> None:
        earliest, latest = DEFAULT_FEE_MODEL.settlement_window(date(2026, 8, 20))
        assert earliest <= latest

    @given(offset=st.integers(min_value=0, max_value=400))
    def test_business_days_after_always_lands_on_a_business_day(self, offset: int) -> None:
        start = date(2026, 1, 1)
        result = DEFAULT_FEE_MODEL.business_days_after(start, offset)
        assert offset == 0 or DEFAULT_FEE_MODEL.is_business_day(result)
