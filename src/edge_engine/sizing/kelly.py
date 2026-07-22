"""Fractional Kelly sizing for binary contracts.

Buying a contract at price c that pays $1: you risk c to win (1-c), so the odds
received are b = (1-c)/c and

    f* = (b*p - (1-p)) / b  =  p - (1-p)*c/(1-c)

At p == c this is exactly zero - no edge, no bet - which is the property that
makes it safe to feed slightly-wrong estimates into.

Everything here is QUARTER Kelly by default. Full Kelly is optimal only if you
KNOW p. You are estimating it, and Kelly's penalty for optimistic estimates is
brutal and asymmetric: overbetting by 2x turns a positive-expectancy edge into
long-run ruin, while underbetting merely grows slower.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Sizing:
    contracts: float
    stake: float
    kelly_full: float
    kelly_used: float
    capped_by: Optional[str] = None

    @property
    def is_actionable(self) -> bool:
        return self.contracts > 0 and self.stake > 0


def kelly_fraction(prob: float, price: float) -> float:
    """Full-Kelly fraction of bankroll. Zero when there is no edge."""
    if not (0.0 < price < 1.0) or not (0.0 <= prob <= 1.0):
        return 0.0
    if prob <= price:
        return 0.0
    return prob - (1.0 - prob) * price / (1.0 - price)


def size_position(
    prob: float,
    price: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,
    max_single_position_pct: float = 5.0,
    available_capital: Optional[float] = None,
    min_order_size: float = 1.0,
    tick_contracts: float = 1.0,
) -> Sizing:
    """Size a single position under all caps. Never returns a negative stake."""
    full = kelly_fraction(prob, price)
    if full <= 0 or bankroll <= 0:
        return Sizing(0.0, 0.0, full, 0.0, capped_by="no_edge")

    used = full * kelly_multiplier
    capped_by = None

    ceiling = max_single_position_pct / 100.0
    if used > ceiling:
        used, capped_by = ceiling, "max_single_position_pct"

    stake = bankroll * used
    if available_capital is not None and stake > available_capital:
        stake, capped_by = max(available_capital, 0.0), "available_capital"

    contracts = stake / price if price > 0 else 0.0
    contracts = (contracts // tick_contracts) * tick_contracts

    if contracts < min_order_size:
        return Sizing(0.0, 0.0, full, used, capped_by="below_min_order_size")

    return Sizing(
        contracts=contracts,
        stake=round(contracts * price, 2),
        kelly_full=full,
        kelly_used=used,
        capped_by=capped_by,
    )
