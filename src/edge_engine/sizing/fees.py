"""Exact per-venue fee math.

Every downstream number depends on this module being right. A sign error here
turns a losing trade into a reported "arb", so the formulas are transcribed
literally from each venue's published schedule and unit-tested against worked
examples.

Kalshi
------
    fee = ceil(0.07 * C * P * (1-P) * 100) / 100

The ceiling applies to the ORDER TOTAL, not per contract. That makes it a
small-size penalty: one contract always costs a full cent, while 100 contracts
pay the true rate. Maker fees are ~25% of taker.

Polymarket
----------
    fee = C * feeRate * P * (1-P)

Rounded to 5dp, no ceiling penalty. `feeRate` varies by category. Makers pay
nothing, and sells are not charged - the fee lands on entry only.
"""
from __future__ import annotations

import math
from typing import Optional

from ..ingest.models import Venue

KALSHI_TAKER_RATE = 0.07
KALSHI_MAKER_MULTIPLIER = 0.25

# Verified against the live Polymarket fee schedule, 2026-07-21.
POLYMARKET_FEE_RATES: dict[str, float] = {
    "geopolitics": 0.00,
    "politics": 0.04,
    "finance": 0.04,
    "tech": 0.04,
    "mentions": 0.04,
    "sports": 0.05,
    "economics": 0.05,
    "culture": 0.05,
    "weather": 0.05,
    "other": 0.05,
    "crypto": 0.07,
}
POLYMARKET_DEFAULT_RATE = 0.05


def polymarket_rate(category: Optional[str], override: Optional[float] = None) -> float:
    """Resolve the Polymarket taker rate.

    `override` carries the per-market rate from `feeSchedule.rate` when present,
    which is authoritative - the category table is only a fallback.

    The bounds check is not paranoia. Polymarket also exposes `takerBaseFee`,
    which reads like a rate but carries an internal unit (observed: 1000).
    Passing that through unguarded inflated computed fees ~1000x and silently
    rejected every candidate in the universe. An out-of-range rate must fail
    loudly to the category table, never flow into the math.
    """
    if override is not None:
        rate = float(override)
        if 0.0 <= rate <= 1.0:
            return rate
        raise ValueError(
            f"implausible Polymarket fee rate {rate!r} - expected a decimal "
            f"fraction in [0, 1]. Check that feeSchedule.rate is being read "
            f"rather than takerBaseFee."
        )
    if not category:
        return POLYMARKET_DEFAULT_RATE
    return POLYMARKET_FEE_RATES.get(category.strip().lower(), POLYMARKET_DEFAULT_RATE)


def kalshi_fee(price: float, contracts: float, maker: bool = False) -> float:
    """Kalshi fee in dollars for an order of `contracts` at `price`."""
    if contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    rate = KALSHI_TAKER_RATE * (KALSHI_MAKER_MULTIPLIER if maker else 1.0)
    raw = rate * contracts * price * (1.0 - price)
    # Snap to 9dp before the ceiling. Binary float makes 0.07*100*0.70*0.30
    # evaluate to 1.4700000000000002, which would ceil to $1.48 instead of
    # $1.47 - systematically overstating Kalshi fees and causing the scanner
    # to reject arbs that are genuinely profitable.
    cents = round(raw * 100.0, 9)
    return math.ceil(cents) / 100.0


def polymarket_fee(
    price: float,
    contracts: float,
    category: Optional[str] = None,
    rate_override: Optional[float] = None,
    maker: bool = False,
    is_sell: bool = False,
) -> float:
    """Polymarket fee in dollars. Makers and sells are free."""
    if maker or is_sell or contracts <= 0 or not (0.0 < price < 1.0):
        return 0.0
    rate = polymarket_rate(category, rate_override)
    if rate <= 0:
        return 0.0
    fee = contracts * rate * price * (1.0 - price)
    return 0.0 if fee < 0.00001 else round(fee, 5)


def fee_for(
    venue: Venue,
    price: float,
    contracts: float,
    category: Optional[str] = None,
    rate_override: Optional[float] = None,
    maker: bool = False,
    is_sell: bool = False,
) -> float:
    """Venue-dispatching fee calculation."""
    if venue is Venue.KALSHI:
        if is_sell:
            return 0.0
        return kalshi_fee(price, contracts, maker=maker)
    return polymarket_fee(
        price, contracts, category=category, rate_override=rate_override,
        maker=maker, is_sell=is_sell,
    )


def effective_rate(venue: Venue, category: Optional[str] = None,
                   rate_override: Optional[float] = None) -> float:
    """The `rate` coefficient in `rate * p * (1-p)`, ignoring Kalshi's ceiling."""
    if venue is Venue.KALSHI:
        return KALSHI_TAKER_RATE
    return polymarket_rate(category, rate_override)


def min_viable_gross_edge(prices: list[float], venue: Venue,
                          category: Optional[str] = None,
                          rate_override: Optional[float] = None) -> float:
    """Gross edge a multi-leg arb must clear before it makes money.

    For an n-leg mutually exclusive set, total fee per unit set is:

        rate * sum(p_i * (1 - p_i))

    This is the result that decides where combinatorial arb is worth running.
    For n roughly equal legs at p = 1/n, sum(p(1-p)) -> 1 - 1/n, which approaches
    1 as n grows. So fee cost converges on the FULL fee rate no matter how many
    legs there are:

        Kalshi (0.07)                -> needs ~7% gross edge
        Polymarket politics (0.04)   -> needs ~4%
        Polymarket geopolitics (0.00)-> needs ANY positive edge

    Which is why a geopolitics negRisk set with sum(YES asks) < 1 is the single
    highest-value target in the system, and why 8-leg Kalshi arbs are a trap.
    """
    rate = effective_rate(venue, category, rate_override)
    return rate * sum(p * (1.0 - p) for p in prices if 0.0 < p < 1.0)


def total_fees(legs: list[tuple[float, float]], venue: Venue,
               category: Optional[str] = None,
               rate_override: Optional[float] = None,
               maker: bool = False) -> float:
    """Summed fees for a multi-leg basket of (price, contracts)."""
    return sum(
        fee_for(venue, price, qty, category=category,
                rate_override=rate_override, maker=maker)
        for price, qty in legs
    )


def cheaper_venue(
    price: float,
    contracts: float,
    category: Optional[str],
    poly_rate_override: Optional[float] = None,
) -> tuple[Venue, float, float]:
    """Fee-aware venue routing for an equivalent contract on both venues.

    Returns (winning_venue, saving_in_dollars, saving_as_pct_of_notional).

    This is not a strategy - it is declining to overpay. Kalshi charges 0.07
    across the board; Polymarket charges 0.04 on politics/finance/tech and
    nothing on geopolitics. Routing correctly is free money on every trade and
    it compounds over a year of them.
    """
    k = kalshi_fee(price, contracts)
    p = polymarket_fee(price, contracts, category=category,
                       rate_override=poly_rate_override)
    notional = price * contracts
    if k <= p:
        return Venue.KALSHI, p - k, ((p - k) / notional if notional else 0.0)
    return Venue.POLYMARKET, k - p, ((k - p) / notional if notional else 0.0)
