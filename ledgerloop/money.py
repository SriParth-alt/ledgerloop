"""Integer-paise money arithmetic.

Every rupee amount in LedgerLoop is an ``int`` number of paise. There are no floats
and no Decimals in storage or in any matching path.

Why: floating point cannot represent 0.1 exactly, so ``0.1 + 0.2 != 0.3``. In a
reconciliation system that compares amounts for equality within a tolerance, float
drift silently becomes false matches and false exceptions. Integer paise makes every
comparison exact and every rounding decision explicit.

If you find a ``float`` anywhere in a money path, that is a defect.
"""

from __future__ import annotations

PAISE_PER_RUPEE = 100


def rupees_to_paise(rupees: str | int) -> int:
    """Parse a rupee string like '1234.56' or an int into integer paise.

    Accepts at most two decimal places. Rejects anything ambiguous rather than
    guessing — a malformed amount should become a quarantined row, not a silent zero.
    """
    if isinstance(rupees, int):
        return rupees * PAISE_PER_RUPEE

    text = rupees.strip().replace(",", "").replace("\u20b9", "")
    if not text:
        raise ValueError("empty amount")

    negative = text.startswith("-")
    if negative:
        text = text[1:]

    if "." not in text:
        whole, frac = text, "00"
    else:
        whole, _, frac = text.partition(".")
        if not frac:
            raise ValueError(f"trailing decimal point with no digits: {rupees!r}")
        if len(frac) > 2:
            raise ValueError(f"more than two decimal places: {rupees!r}")
        frac = frac.ljust(2, "0")

    if not whole.isdigit() or not frac.isdigit():
        raise ValueError(f"not a valid amount: {rupees!r}")

    paise = int(whole) * PAISE_PER_RUPEE + int(frac)
    return -paise if negative else paise


def paise_to_rupees(paise: int) -> str:
    """Format integer paise as a rupee string with exactly two decimal places."""
    sign = "-" if paise < 0 else ""
    magnitude = abs(paise)
    return f"{sign}{magnitude // PAISE_PER_RUPEE}.{magnitude % PAISE_PER_RUPEE:02d}"


def apply_rate(base_paise: int, rate_bps: int) -> int:
    """Apply a basis-point rate to a paise amount, rounding half away from zero.

    ``rate_bps`` is basis points: 200 bps = 2.00%. Basis points are used instead of a
    float percentage so the rate itself is exact.

    Rounding is half-away-from-zero (not banker's rounding) because that is what
    Indian payment gateways and bank statements do in practice, and reconciliation
    must reproduce the counterparty's arithmetic rather than the mathematically
    tidier convention.
    """
    if rate_bps < 0:
        raise ValueError("rate_bps must be non-negative")

    numerator = abs(base_paise) * rate_bps
    quotient, remainder = divmod(numerator, 10_000)
    if remainder * 2 >= 10_000:
        quotient += 1

    return -quotient if base_paise < 0 else quotient


def within_tolerance(a_paise: int, b_paise: int, *, abs_paise: int, rel_bps: int) -> bool:
    """True if two amounts agree within the larger of an absolute or relative band.

    Reconciliation tolerance has to be both: a flat band alone is too tight on large
    settlements, and a relative band alone is too tight on small ones.
    """
    delta = abs(a_paise - b_paise)
    relative_allowance = apply_rate(max(abs(a_paise), abs(b_paise)), rel_bps)
    return delta <= max(abs_paise, relative_allowance)
