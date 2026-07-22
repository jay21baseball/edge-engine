"""Common Signal type emitted by every strategy."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from ..ingest.models import Venue


@dataclass
class Leg:
    """One executable leg of a signal - everything needed to place it by hand."""

    venue: Venue
    market_id: str
    title: str
    side: str
    limit_price: float
    contracts: float
    est_fee: float = 0.0

    @property
    def notional(self) -> float:
        return self.limit_price * self.contracts


@dataclass
class Signal:
    """A scored opportunity. Never an instruction - always a proposal with its math shown."""

    strategy: str
    venue: Venue
    market_id: str
    title: str
    side: str
    entry_price: float
    est_probability: float
    edge: float                      # fractional, net of fees
    confidence: float                # 0..1
    days_to_resolution: float
    category: str = ""
    legs: list[Leg] = field(default_factory=list)
    rationale: dict[str, Any] = field(default_factory=dict)
    counter_case: str = ""
    deterministic: bool = False      # True = arithmetic, not a forecast
    ts: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    stake: Optional[float] = None
    contracts: Optional[float] = None

    @property
    def score(self) -> float:
        """Edge per day of capital lockup.

        The ranking key for the whole system. At a small bankroll, capital
        velocity dominates edge size: a 3% edge resolving in 2 days recycles
        ~180x/year, an 8% edge resolving in 8 months recycles 1.5x. Ranking on
        raw edge would fill the book with slow political markets and freeze the
        stack. Deterministic arbs get a multiplier because their edge is
        arithmetic rather than an estimate that might be wrong.
        """
        base = (self.edge * self.confidence) / max(self.days_to_resolution, 1.0 / 24.0)
        return base * (1.5 if self.deterministic else 1.0)

    @property
    def total_notional(self) -> float:
        return sum(leg.notional for leg in self.legs) or (self.stake or 0.0)


class Strategy:
    """Strategies are independent: adding one touches no other module."""

    name: str = "base"
    enabled: bool = True
    min_bankroll: float = 0.0

    def scan(self, context: Any) -> list[Signal]:
        raise NotImplementedError
