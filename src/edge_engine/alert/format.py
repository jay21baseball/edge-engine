"""American odds conversion and betting-style presentation.

Prediction markets quote in cents (a contract at 45c pays $1). Sportsbooks quote
in American odds. Same information, and the cent price is more precise, but
American odds make the risk/reward obvious at a glance - which is the point of
the whole briefing.

    price 0.45  ->  +122   (risk 100 to win 122)
    price 0.65  ->  -186   (risk 186 to win 100)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def to_american(price: float) -> Optional[int]:
    """Contract price (0..1) -> American odds.

    A contract costing `price` pays $1, so you risk `price` to win `1 - price`.
    Underdogs (price < 0.50) get positive odds, favourites negative.
    """
    if not (0.0 < price < 1.0):
        return None
    if price < 0.5:
        return round((1.0 - price) / price * 100)
    return -round(price / (1.0 - price) * 100)


def american_str(price: float) -> str:
    odds = to_american(price)
    if odds is None:
        return "n/a"
    return f"+{odds}" if odds > 0 else str(odds)


def from_american(odds: int) -> Optional[float]:
    """American odds -> implied probability. Inverse of to_american."""
    if odds == 0:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return -odds / (-odds + 100.0)


def cents(price: float) -> str:
    return f"{price * 100:.1f}¢"


def price_line(price: float) -> str:
    """Both notations together: '+122 (45.0c)'."""
    return f"{american_str(price)} ({cents(price)})"


def payout(stake: float, price: float) -> float:
    """Profit if the contract resolves YES."""
    if not (0.0 < price < 1.0) or stake <= 0:
        return 0.0
    return stake * (1.0 - price) / price


def edge_points(fair: float, market: float) -> float:
    """Edge in probability points - the unit bettors actually think in."""
    return (fair - market) * 100.0


def horizon(days: float) -> str:
    if days < 1.0 / 24:
        return "minutes"
    if days < 1.0:
        return f"{days * 24:.0f}h"
    if days < 14:
        return f"{days:.0f}d"
    if days < 90:
        return f"{days / 7:.0f}wk"
    return f"{days / 30:.0f}mo"


def money(value: Optional[float]) -> str:
    return "-" if value is None else f"${value:,.2f}"


def resolve_date(days: float, now: Optional[datetime] = None) -> str:
    from datetime import timedelta
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(days=days)).strftime("%b %d")


def confidence_bar(confidence: float, width: int = 5) -> str:
    filled = max(0, min(width, round(confidence * width)))
    return "█" * filled + "░" * (width - filled)


def grade(score: float, edge: float, confidence: float,
          deterministic: bool) -> str:
    """A single letter so the ranking is legible without reading the numbers.

    Deterministic arbs start at A because the payout is arithmetic; everything
    else has to earn its grade through edge AND confidence, since a large edge
    at low confidence is usually a data problem rather than an opportunity.
    """
    if deterministic:
        return "A+" if edge >= 0.03 else "A"
    quality = edge * confidence
    if quality >= 0.06:
        return "A"
    if quality >= 0.035:
        return "B"
    if quality >= 0.02:
        return "C"
    return "D"
