"""Kalshi client. Public market data only - no auth, no order placement.

Base host verified live 2026-07-21. The documented `trading-api.kalshi.com` and
`external-api.kalshi.com` hosts are stale; `api.elections.kalshi.com` is live.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterator, Optional

from .http import RateLimiter, request_json, safe_float
from .models import Event, Market, OrderBook, PriceLevel, Venue

log = logging.getLogger(__name__)

BASE = "https://api.elections.kalshi.com/trade-api/v2"


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _price(raw: dict, *names: str) -> Optional[float]:
    """Kalshi is mid-migration from cents to `_dollars` fields. Accept both."""
    for name in names:
        if name in raw and raw[name] is not None:
            val = safe_float(raw[name], -1.0)
            if val < 0:
                continue
            # Legacy cent-denominated fields arrive as integers 0..100.
            return val / 100.0 if not name.endswith("_dollars") and val > 1.0 else val
    return None


class KalshiClient:
    def __init__(self, rate_per_sec: float = 8.0):
        self.limiter = RateLimiter(rate_per_sec, burst=rate_per_sec * 2)

    def _get(self, path: str, **params) -> Any:
        return request_json(f"{BASE}{path}", params=params, limiter=self.limiter)

    def _paginate(self, path: str, key: str, page_limit: int = 200,
                  max_pages: int = 60, **params) -> Iterator[dict]:
        cursor = None
        for _ in range(max_pages):
            data = self._get(path, limit=page_limit, cursor=cursor, **params)
            rows = data.get(key) or []
            yield from rows
            cursor = data.get("cursor")
            if not cursor or not rows:
                return

    def parse_market(self, raw: dict, event_id: str = "", category: str = "") -> Market:
        return Market(
            venue=Venue.KALSHI,
            market_id=raw.get("ticker", ""),
            event_id=event_id or raw.get("event_ticker", ""),
            title=raw.get("yes_sub_title") or raw.get("title") or raw.get("ticker", ""),
            category=category,
            yes_bid=_price(raw, "yes_bid_dollars", "yes_bid"),
            yes_ask=_price(raw, "yes_ask_dollars", "yes_ask"),
            no_bid=_price(raw, "no_bid_dollars", "no_bid"),
            no_ask=_price(raw, "no_ask_dollars", "no_ask"),
            volume=safe_float(raw.get("volume_fp") or raw.get("volume")),
            liquidity=safe_float(raw.get("liquidity_dollars") or raw.get("liquidity")),
            close_ts=_ts(raw.get("close_time")),
            status=raw.get("status", "open"),
            fee_rate=0.07,
            min_order_size=1.0,
            tick_size=0.01,
            raw=raw,
        )

    def events(self, status: str = "open", with_markets: bool = True) -> list[Event]:
        out: list[Event] = []
        for raw in self._paginate("/events", "events", status=status,
                                  with_nested_markets=str(with_markets).lower()):
            category = raw.get("category", "") or ""
            event_id = raw.get("event_ticker", "")
            markets = [
                self.parse_market(m, event_id, category)
                for m in (raw.get("markets") or [])
            ]
            out.append(Event(
                venue=Venue.KALSHI,
                event_id=event_id,
                title=raw.get("title", ""),
                category=category,
                # Read the venue's own flag. Never infer exclusivity from titles.
                mutually_exclusive=bool(raw.get("mutually_exclusive")),
                neg_risk=False,
                markets=markets,
                raw={k: v for k, v in raw.items() if k != "markets"},
            ))
        return out

    def markets(self, status: str = "open") -> list[Market]:
        return [self.parse_market(m) for m in self._paginate("/markets", "markets",
                                                             status=status)]

    def orderbook(self, ticker: str, depth: int = 12) -> OrderBook:
        raw = self._get(f"/markets/{ticker}/orderbook", depth=depth)
        return self.parse_orderbook(ticker, raw)

    @staticmethod
    def parse_orderbook(ticker: str, raw: dict) -> OrderBook:
        """Convert Kalshi's bids-only book into true two-sided quotes.

        Kalshi returns BID ladders for both sides and no asks whatsoever:

            {"orderbook_fp": {"yes_dollars": [[price, size], ...],
                              "no_dollars":  [[price, size], ...]}}

        A YES ask exists only as somebody's NO bid, so:

            yes_ask = 1 - no_bid        no_ask = 1 - yes_bid

        Verified against live data: a market quoting yes_bid=0.08 / yes_ask=0.14
        had a best NO bid of 0.86, and 1 - 0.86 = 0.14.

        Reading `yes_dollars` as an ask ladder - the obvious mistake, and the one
        most published scanners make - produces prices that are wrong by the
        entire width of the market.
        """
        book = raw.get("orderbook_fp") or raw.get("orderbook") or {}

        def ladder(key: str) -> list[PriceLevel]:
            levels = []
            for row in (book.get(key) or []):
                if isinstance(row, (list, tuple)) and len(row) >= 2:
                    price, size = safe_float(row[0]), safe_float(row[1])
                    if price > 1.0:  # legacy cents
                        price /= 100.0
                    if 0.0 < price < 1.0 and size > 0:
                        levels.append(PriceLevel(price, size))
            return sorted(levels, key=lambda lv: -lv.price)  # best bid first

        yes_bids = ladder("yes_dollars") or ladder("yes")
        no_bids = ladder("no_dollars") or ladder("no")

        def invert(bids: list[PriceLevel]) -> list[PriceLevel]:
            asks = [PriceLevel(round(1.0 - lv.price, 6), lv.size) for lv in bids]
            return sorted(asks, key=lambda lv: lv.price)  # best (lowest) ask first

        return OrderBook(
            market_id=ticker,
            venue=Venue.KALSHI,
            yes_bids=yes_bids,
            no_bids=no_bids,
            yes_asks=invert(no_bids),
            no_asks=invert(yes_bids),
        )
