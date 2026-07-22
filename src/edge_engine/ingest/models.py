"""Normalized cross-venue data model.

Kalshi and Polymarket disagree about almost everything: field names, price units,
whether an order book returns asks, whether a list is a list or a JSON string.
Everything downstream of ingest speaks these types only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional


class Venue(str, Enum):
    KALSHI = "kalshi"
    POLYMARKET = "polymarket"


class Side(str, Enum):
    YES = "yes"
    NO = "no"


@dataclass(frozen=True)
class PriceLevel:
    """One rung of an order book ladder. Price in dollars (0..1), size in contracts."""

    price: float
    size: float


@dataclass
class OrderBook:
    """Normalized book. Both venues are coerced into true asks and bids.

    Kalshi returns bid ladders for BOTH sides and no asks at all; the YES ask is
    derived as (1 - best NO bid). See ingest.kalshi.parse_orderbook.
    """

    market_id: str
    venue: Venue
    yes_bids: list[PriceLevel] = field(default_factory=list)
    yes_asks: list[PriceLevel] = field(default_factory=list)
    no_bids: list[PriceLevel] = field(default_factory=list)
    no_asks: list[PriceLevel] = field(default_factory=list)
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def asks(self, side: Side) -> list[PriceLevel]:
        return self.yes_asks if side is Side.YES else self.no_asks

    def bids(self, side: Side) -> list[PriceLevel]:
        return self.yes_bids if side is Side.YES else self.no_bids

    def best_ask(self, side: Side) -> Optional[float]:
        levels = self.asks(side)
        return levels[0].price if levels else None

    def cost_to_fill(self, side: Side, qty: float) -> Optional[tuple[float, float]]:
        """Walk the ask ladder to buy `qty` contracts.

        Returns (total_cost_dollars, average_price), or None if the book cannot
        fill the requested size. Returning None rather than a partial fill is
        deliberate: a partial fill on one leg of an arb is a naked position.
        """
        remaining, cost = qty, 0.0
        for level in self.asks(side):
            if remaining <= 0:
                break
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
        if remaining > 1e-9:
            return None
        return cost, cost / qty if qty else 0.0

    def depth_available(self, side: Side, max_price: float) -> float:
        """Total size purchasable at or below `max_price`."""
        return sum(lv.size for lv in self.asks(side) if lv.price <= max_price + 1e-9)


@dataclass
class Market:
    """A single binary contract on either venue."""

    venue: Venue
    market_id: str
    event_id: str
    title: str
    category: str
    yes_bid: Optional[float] = None
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    volume: float = 0.0
    liquidity: float = 0.0
    close_ts: Optional[datetime] = None
    status: str = "open"
    fee_rate: Optional[float] = None
    min_order_size: float = 1.0
    tick_size: float = 0.01
    raw: dict = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.venue.value}:{self.market_id}"

    @property
    def days_to_resolution(self) -> float:
        """Days until close. Floored at ~1 hour so same-day markets don't divide by zero."""
        if not self.close_ts:
            return 365.0
        delta = (self.close_ts - datetime.now(timezone.utc)).total_seconds() / 86400.0
        return max(delta, 1.0 / 24.0)

    def price(self, side: Side) -> Optional[float]:
        return self.yes_ask if side is Side.YES else self.no_ask


@dataclass
class Event:
    """A group of markets. Mutual exclusivity is read from the venue flag, never inferred."""

    venue: Venue
    event_id: str
    title: str
    category: str
    mutually_exclusive: bool = False
    neg_risk: bool = False
    markets: list[Market] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @property
    def is_mece(self) -> bool:
        """Mutually exclusive AND collectively exhaustive, per the venue's own flag.

        Polymarket signals this with negRisk (backed by the on-chain adapter).
        Kalshi signals it with mutually_exclusive on the event.
        """
        return (self.neg_risk or self.mutually_exclusive) and len(self.markets) >= 2


@dataclass
class WalletPosition:
    address: str
    market_id: str
    title: str
    side: Side
    size: float
    avg_price: float
    current_price: float = 0.0
    realized_pnl: float = 0.0
    cash_pnl: float = 0.0
    redeemable: bool = False
    observed_ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class WalletActivity:
    address: str
    ts: datetime
    market_id: str
    title: str
    side: str
    action: str
    size: float
    usdc_size: float
    price: float


@dataclass
class LeaderboardEntry:
    address: str
    username: str
    rank: int
    pnl: float
    volume: float
    window: str
    category: str

    @property
    def volume_to_pnl(self) -> float:
        """Turnover per dollar earned.

        The single most discriminating market-maker tell. Measured live on the
        2026-07-21 top 25: directional traders cluster 2-8x, market makers 35-155x.
        """
        if self.pnl <= 0:
            return float("inf")
        return self.volume / self.pnl
