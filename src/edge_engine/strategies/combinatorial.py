"""Within-venue combinatorial arbitrage on mutually exclusive market sets.

For a MECE set of n binary markets, exactly one resolves YES:

    buy every YES:  pay sum(yes_ask),  receive $1        -> edge = 1 - sum(yes_ask)
    buy every NO:   pay sum(no_ask),   receive $(n-1)    -> edge = (n-1) - sum(no_ask)

Both are arithmetic, not forecasts. The entire difficulty is refusing to believe
your own scanner: quoted top-of-book prices routinely imply arbs that vanish the
moment you price real fees or try to fill real size.

Two-phase by design. Phase 1 screens on already-fetched quotes (free). Phase 2
pulls full order books only for survivors, because books cost one API call per
leg and most candidates die in phase 1.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..ingest.models import Event, Market, OrderBook, Side, Venue
from ..sizing.fees import fee_for, min_viable_gross_edge
from .base import Leg, Signal, Strategy

log = logging.getLogger(__name__)

# Legs must settle together or it is timing risk, not arbitrage.
MAX_RESOLUTION_SKEW_DAYS = 1.0


class CombinatorialArb(Strategy):
    name = "combinatorial_arb"
    min_bankroll = 0.0

    def __init__(self, min_net_edge: float = 0.005, max_legs: int = 20,
                 target_contracts: float = 100.0):
        self.min_net_edge = min_net_edge
        self.max_legs = max_legs
        self.target_contracts = target_contracts

    # ------------------------------------------------------------- phase one

    def screen(self, event: Event) -> Optional[dict]:
        """Cheap pre-filter on quoted prices. Returns a candidate dict or None."""
        if not event.is_mece:
            return None
        markets = [m for m in event.markets if m.status == "open"]
        if not (2 <= len(markets) <= self.max_legs):
            return None
        if not self._resolutions_aligned(markets):
            return None

        yes_asks = [m.yes_ask for m in markets]
        no_asks = [m.no_ask for m in markets]
        n = len(markets)

        best = None
        if all(a is not None and 0 < a < 1 for a in yes_asks):
            gross = 1.0 - sum(yes_asks)
            if gross > 0:
                best = {"direction": Side.YES, "gross": gross,
                        "prices": list(yes_asks)}
        if all(a is not None and 0 < a < 1 for a in no_asks):
            gross = (n - 1) - sum(no_asks)
            if gross > 0 and (best is None or gross > best["gross"]):
                best = {"direction": Side.NO, "gross": gross,
                        "prices": list(no_asks)}
        if best is None:
            return None

        # Reject before spending API calls if fees cannot possibly be covered.
        # For n roughly-equal legs this burden converges on the venue's full fee
        # rate, which is why 8-leg Kalshi sets are almost always a trap and
        # zero-fee Polymarket geopolitics sets almost always survive.
        category = event.category
        rate_override = markets[0].fee_rate if event.venue is Venue.POLYMARKET else None
        burden = min_viable_gross_edge(best["prices"], event.venue,
                                       category=category, rate_override=rate_override)
        if best["gross"] <= burden:
            log.debug("%s: gross %.4f <= fee burden %.4f, rejected in phase 1",
                      event.title[:50], best["gross"], burden)
            return None

        best.update(event=event, markets=markets, fee_burden=burden)
        return best

    @staticmethod
    def _resolutions_aligned(markets: list[Market]) -> bool:
        stamps = [m.close_ts for m in markets if m.close_ts]
        if len(stamps) < len(markets):
            return False
        spread_days = (max(stamps) - min(stamps)).total_seconds() / 86400.0
        return spread_days <= MAX_RESOLUTION_SKEW_DAYS

    # ------------------------------------------------------------- phase two

    def verify(self, candidate: dict,
               book_fetcher: Callable[[Market], Optional[OrderBook]],
               contracts: Optional[float] = None) -> Optional[Signal]:
        """Re-price against real order books at real size.

        Returns None if the book cannot fill every leg. A partial fill on a
        combinatorial arb is not a smaller arb - it is a naked directional
        position, which is precisely the risk the strategy claims to avoid.
        """
        event: Event = candidate["event"]
        markets: list[Market] = candidate["markets"]
        side: Side = candidate["direction"]
        qty = contracts or self.target_contracts
        n = len(markets)

        legs: list[Leg] = []
        total_cost = 0.0
        total_fees = 0.0

        for market in markets:
            book = book_fetcher(market)
            if book is None:
                return None
            fill = book.cost_to_fill(side, qty)
            if fill is None:
                log.debug("%s leg %s: insufficient depth for %s contracts",
                          event.title[:40], market.market_id, qty)
                return None
            cost, avg_price = fill
            fee = fee_for(
                event.venue, avg_price, qty,
                category=event.category,
                rate_override=market.fee_rate if event.venue is Venue.POLYMARKET
                else None,
            )
            total_cost += cost
            total_fees += fee
            legs.append(Leg(
                venue=event.venue, market_id=market.market_id,
                title=market.title, side=side.value,
                limit_price=round(avg_price, 4), contracts=qty, est_fee=round(fee, 4),
            ))

        payout = qty * (1.0 if side is Side.YES else (n - 1))
        net_profit = payout - total_cost - total_fees
        if total_cost <= 0:
            return None
        net_edge = net_profit / total_cost
        if net_edge < self.min_net_edge:
            return None

        return Signal(
            strategy=self.name,
            venue=event.venue,
            market_id=event.event_id,
            title=event.title,
            side=f"buy_all_{side.value}",
            entry_price=round(total_cost / qty, 4),
            est_probability=1.0,
            edge=net_edge,
            confidence=1.0,
            days_to_resolution=min(m.days_to_resolution for m in markets),
            category=event.category,
            legs=legs,
            deterministic=True,
            rationale={
                "legs": n,
                "gross_edge_quoted": round(candidate["gross"], 4),
                "fee_burden_threshold": round(candidate["fee_burden"], 4),
                "cost_at_size": round(total_cost, 2),
                "fees_at_size": round(total_fees, 2),
                "guaranteed_payout": round(payout, 2),
                "net_profit": round(net_profit, 2),
                "neg_risk_enforced": event.neg_risk,
                "contracts_per_leg": qty,
            },
            counter_case=(
                "Requires ALL legs to fill at these prices. A partial fill leaves a "
                "naked directional position. "
                + ("negRisk adapter enforces the payout identity on-chain."
                   if event.neg_risk else
                   "Not negRisk-backed: verify the set is genuinely exhaustive and "
                   "that no leg can resolve early or be voided.")
            ),
        )

    # ---------------------------------------------------------------- driver

    def scan(self, context) -> list[Signal]:
        signals: list[Signal] = []
        for event in context.events:
            candidate = self.screen(event)
            if candidate is None:
                continue
            try:
                signal = self.verify(candidate, context.book_fetcher)
            except Exception as e:
                log.warning("verify failed for %s: %s", event.event_id, e)
                continue
            if signal:
                signals.append(signal)
        return sorted(signals, key=lambda s: -s.score)
