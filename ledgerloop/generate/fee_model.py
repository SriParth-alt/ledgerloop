"""Payment-gateway fee model and settlement timing.

This module is the single source of truth for the arithmetic that makes bank credits
disagree with invoice amounts:

    net = gross - mdr_fee - gst_on_fee - tds

It is used in two directions:

* by the **generator**, to produce realistic synthetic settlements, and
* by **Tier 1** of the cascade, to recompute the expected net for a candidate match.

Both directions must use the same code. If they drift, the matcher will appear to
work on synthetic data for the wrong reason.

NOTE ON REALISM: the rates below are plausible defaults for an Indian PSP, not a
quotation of any real published schedule, and the TDS treatment is simplified. They
are configurable precisely because the exact numbers are not the point — the point is
that a fixed, knowable function separates gross from net. Do not present these as
real Razorpay pricing in the write-up or the pitch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from ledgerloop.money import apply_rate


class PaymentMethod(StrEnum):
    UPI = "upi"
    DEBIT_CARD = "debit_card"
    CREDIT_CARD = "credit_card"
    NETBANKING = "netbanking"
    WALLET = "wallet"


@dataclass(frozen=True)
class MethodPricing:
    """Pricing for one payment method.

    ``rate_bps`` is basis points on gross; ``flat_paise`` is a per-transaction fixed
    component; ``cap_paise`` optionally caps the percentage component.
    """

    rate_bps: int
    flat_paise: int = 0
    cap_paise: int | None = None


@dataclass(frozen=True)
class FeeModel:
    """Configurable fee and settlement-timing model."""

    pricing: dict[PaymentMethod, MethodPricing] = field(
        default_factory=lambda: {
            PaymentMethod.UPI: MethodPricing(rate_bps=0),
            PaymentMethod.DEBIT_CARD: MethodPricing(rate_bps=40, cap_paise=100_000),
            PaymentMethod.CREDIT_CARD: MethodPricing(rate_bps=200),
            PaymentMethod.NETBANKING: MethodPricing(rate_bps=0, flat_paise=1_800),
            PaymentMethod.WALLET: MethodPricing(rate_bps=200),
        }
    )
    gst_bps: int = 1_800
    """GST charged on the fee itself (18%), not on the gross."""

    tds_bps: int = 10
    """Simplified TDS withheld on gross. Set to 0 to disable."""

    settlement_lag_days: int = 2
    """T+N in business days."""

    holidays: frozenset[date] = frozenset()

    # -- fee components -----------------------------------------------------

    def mdr_fee_paise(self, gross_paise: int, method: PaymentMethod) -> int:
        pricing = self.pricing[method]
        percentage = apply_rate(gross_paise, pricing.rate_bps)
        if pricing.cap_paise is not None:
            percentage = min(percentage, pricing.cap_paise)
        return percentage + pricing.flat_paise

    def gst_on_fee_paise(self, fee_paise: int) -> int:
        return apply_rate(fee_paise, self.gst_bps)

    def tds_paise(self, gross_paise: int) -> int:
        return apply_rate(gross_paise, self.tds_bps)

    def net_paise(self, gross_paise: int, method: PaymentMethod) -> int:
        """The amount that actually reaches the bank account."""
        fee = self.mdr_fee_paise(gross_paise, method)
        gst = self.gst_on_fee_paise(fee)
        tds = self.tds_paise(gross_paise)
        return gross_paise - fee - gst - tds

    def breakdown(self, gross_paise: int, method: PaymentMethod) -> dict[str, int]:
        """Every component, for provenance records and for the exception UI.

        When a Tier 1 match fails on amount, the associate needs to see *which*
        component the model expected — that is what turns an exception into a
        diagnosable one.
        """
        fee = self.mdr_fee_paise(gross_paise, method)
        gst = self.gst_on_fee_paise(fee)
        tds = self.tds_paise(gross_paise)
        return {
            "gross_paise": gross_paise,
            "fee_paise": fee,
            "gst_on_fee_paise": gst,
            "tds_paise": tds,
            "net_paise": gross_paise - fee - gst - tds,
        }

    # -- settlement timing --------------------------------------------------

    def is_business_day(self, day: date) -> bool:
        return day.weekday() < 5 and day not in self.holidays

    def business_days_after(self, start: date, days: int) -> date:
        """Add N business days, skipping weekends and configured holidays.

        All settlement-date arithmetic goes through here. Inline date math is how
        a matcher ends up with a window that is correct in August and wrong in
        October.
        """
        if days < 0:
            raise ValueError("days must be non-negative")
        current = start
        remaining = days
        while remaining > 0:
            current += timedelta(days=1)
            if self.is_business_day(current):
                remaining -= 1
        return current

    def settlement_date(self, captured_on: date) -> date:
        return self.business_days_after(captured_on, self.settlement_lag_days)

    def settlement_window(self, captured_on: date, slack_days: int = 1) -> tuple[date, date]:
        """The date range in which a credit for this capture could plausibly land.

        Tier 1 accepts a match only inside this window. Slack absorbs the reality
        that settlement cycles slip by a day without anything being wrong.
        """
        earliest = self.settlement_date(captured_on)
        latest = self.business_days_after(earliest, slack_days)
        return earliest, latest


DEFAULT_FEE_MODEL = FeeModel()

#: Public holidays inside the build's generation window.
#:
#: This lives here, beside the business-day arithmetic that consumes it, because two
#: independent callers now need the *same* calendar: the generator, which decides when a
#: settlement lands, and Tier 1, which decides whether a credit arrived inside the
#: plausible window. If those two disagreed by a single holiday, Tier 1 would reject
#: correct matches every time a settlement straddled one — and the failure would look
#: like a tolerance problem rather than a calendar problem.
#:
#: Not a complete Indian holiday calendar. It covers the dates the fixtures can reach
#: and is stated as fixture configuration, not as a claim about the RBI calendar.
INDIAN_HOLIDAYS: frozenset[date] = frozenset(
    {date(2026, 8, 15), date(2026, 9, 17), date(2026, 10, 2)}
)

#: The model both the generator and the cascade use. ``DEFAULT_FEE_MODEL`` is left
#: holiday-free so the property tests keep exercising the arithmetic in isolation.
SETTLEMENT_FEE_MODEL = FeeModel(holidays=INDIAN_HOLIDAYS)
