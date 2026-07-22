"""Polymarket client: Gamma (discovery), CLOB (books), Data API (wallets).

All three are public and unauthenticated. Only order placement needs credentials,
and this system never places orders.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from .http import (
    RateLimiter,
    parse_json_field,
    request_json,
    safe_float,
)
from .models import (
    Event,
    LeaderboardEntry,
    Market,
    OrderBook,
    PriceLevel,
    Side,
    Venue,
    WalletActivity,
    WalletPosition,
)

log = logging.getLogger(__name__)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"

LEADERBOARD_WINDOWS = ("DAY", "WEEK", "MONTH", "ALL")
# GEOPOLITICS is rejected by the leaderboard endpoint ("invalid category
# parameter") even though it is a valid market category.
LEADERBOARD_CATEGORIES = (
    "OVERALL", "POLITICS", "SPORTS", "CRYPTO",
    "ECONOMICS", "CULTURE", "TECH", "WEATHER",
)


def _ts(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc)
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class PolymarketClient:
    def __init__(self):
        self.gamma_limiter = RateLimiter(10.0, burst=20)
        self.clob_limiter = RateLimiter(10.0, burst=20)
        # Positions endpoint is documented at 150 req / 10s. Stay well under.
        self.data_limiter = RateLimiter(12.0, burst=24)

    # ---------------------------------------------------------------- markets

    def parse_market(self, raw: dict, event_id: str = "",
                     category: str = "") -> Market:
        """Normalize a Gamma market.

        `outcomes` and `outcomePrices` arrive as JSON-encoded STRINGS, not arrays -
        a quirk that silently yields empty prices if you index them directly.
        """
        prices = [safe_float(p) for p in parse_json_field(raw.get("outcomePrices"))]
        yes_price = prices[0] if prices else None
        no_price = prices[1] if len(prices) > 1 else (
            1.0 - yes_price if yes_price is not None else None
        )
        best_bid = raw.get("bestBid")
        best_ask = raw.get("bestAsk")

        return Market(
            venue=Venue.POLYMARKET,
            market_id=str(raw.get("id", "")),
            event_id=event_id,
            title=raw.get("groupItemTitle") or raw.get("question", ""),
            category=category,
            yes_bid=safe_float(best_bid) if best_bid is not None else yes_price,
            yes_ask=safe_float(best_ask) if best_ask is not None else yes_price,
            no_bid=(1.0 - safe_float(best_ask)) if best_ask is not None else no_price,
            no_ask=(1.0 - safe_float(best_bid)) if best_bid is not None else no_price,
            volume=safe_float(raw.get("volumeNum") or raw.get("volume")),
            liquidity=safe_float(raw.get("liquidityNum") or raw.get("liquidity")),
            close_ts=_ts(raw.get("endDate") or raw.get("endDateIso")),
            status="closed" if raw.get("closed") else "open",
            fee_rate=self._fee_rate(raw),
            min_order_size=safe_float(raw.get("orderMinSize"), 5.0),
            tick_size=safe_float(raw.get("orderPriceMinTickSize"), 0.01),
            raw=raw,
        )

    @staticmethod
    def _fee_rate(raw: dict) -> Optional[float]:
        """Extract the real decimal taker rate.

        NOT `takerBaseFee` - that field carries an internal unit (observed value
        1000 across every category) and feeding it to the fee formula inflates
        the computed cost by ~1000x, which silently rejects every opportunity in
        the universe while looking like it works.

        The usable rate is `feeSchedule.rate` (0.04 / 0.05 / 0.07 by category,
        0 for geopolitics), alongside `takerOnly` confirming makers pay nothing
        and `rebateRate` giving the maker rebate share.
        """
        if raw.get("feesEnabled") is False:
            return 0.0
        schedule = raw.get("feeSchedule")
        if isinstance(schedule, dict) and schedule.get("rate") is not None:
            rate = safe_float(schedule["rate"], -1.0)
            # A real rate is a small fraction. Anything else is a schema change,
            # and must fall through to the category table rather than poison the
            # math downstream.
            if 0.0 <= rate <= 1.0:
                return rate
            log.warning("implausible feeSchedule.rate=%r on market %s; "
                        "falling back to category table", schedule.get("rate"),
                        raw.get("id"))
        return None

    def events(self, limit: int = 500, closed: bool = False) -> list[Event]:
        out: list[Event] = []
        page, per_page = 0, 100
        while page * per_page < limit:
            rows = request_json(
                f"{GAMMA}/events", limiter=self.gamma_limiter,
                params={"limit": per_page, "offset": page * per_page,
                        "closed": str(closed).lower(), "active": "true",
                        "order": "volume24hr", "ascending": "false"},
            )
            if not isinstance(rows, list) or not rows:
                break
            for raw in rows:
                category = self._category_of(raw)
                event_id = str(raw.get("id", ""))
                markets = [
                    self.parse_market(m, event_id, category)
                    for m in (raw.get("markets") or [])
                ]
                out.append(Event(
                    venue=Venue.POLYMARKET,
                    event_id=event_id,
                    title=raw.get("title", ""),
                    category=category,
                    mutually_exclusive=bool(raw.get("negRisk")),
                    # negRisk is backed by the on-chain CTF adapter: a NO share in
                    # any leg converts to YES in every other leg, which makes the
                    # arb identity mechanically enforced rather than hoped for.
                    neg_risk=bool(raw.get("negRisk")),
                    markets=markets,
                    raw={k: v for k, v in raw.items() if k != "markets"},
                ))
            page += 1
        return out

    @staticmethod
    def _category_of(raw: dict) -> str:
        for tag in (raw.get("tags") or []):
            label = (tag.get("label") or tag.get("slug") or "").strip().lower()
            if label in {
                "politics", "sports", "crypto", "economics", "culture",
                "geopolitics", "tech", "weather", "finance", "mentions",
            }:
                return label
        return (raw.get("category") or "other").strip().lower()

    def orderbook(self, token_id: str) -> OrderBook:
        raw = request_json(f"{CLOB}/book", limiter=self.clob_limiter,
                           params={"token_id": token_id})
        return self.parse_orderbook(token_id, raw)

    @staticmethod
    def parse_orderbook(token_id: str, raw: dict) -> OrderBook:
        """Polymarket books are already two-sided, unlike Kalshi's.

        A token's book is one outcome's book: its `asks` are YES asks and its
        `bids` are YES bids for THAT token. The complementary side is priced at
        (1 - p) against the paired token.
        """
        def ladder(key: str, reverse: bool) -> list[PriceLevel]:
            levels = []
            for row in (raw.get(key) or []):
                price, size = safe_float(row.get("price")), safe_float(row.get("size"))
                if 0.0 < price < 1.0 and size > 0:
                    levels.append(PriceLevel(price, size))
            return sorted(levels, key=lambda lv: -lv.price if reverse else lv.price)

        asks = ladder("asks", reverse=False)   # cheapest first
        bids = ladder("bids", reverse=True)    # highest first
        return OrderBook(
            market_id=token_id, venue=Venue.POLYMARKET,
            yes_asks=asks, yes_bids=bids,
            no_asks=[PriceLevel(round(1 - lv.price, 6), lv.size) for lv in bids],
            no_bids=[PriceLevel(round(1 - lv.price, 6), lv.size) for lv in asks],
        )

    @staticmethod
    def token_ids(market_raw: dict) -> list[str]:
        return [str(t) for t in parse_json_field(market_raw.get("clobTokenIds"))]

    # ---------------------------------------------------------------- wallets

    def leaderboard(self, window: str = "MONTH", category: str = "OVERALL",
                    order_by: str = "PNL", limit: int = 50
                    ) -> list[LeaderboardEntry]:
        rows = request_json(
            f"{DATA}/v1/leaderboard", limiter=self.data_limiter,
            params={"timePeriod": window, "category": category,
                    "orderBy": order_by, "limit": min(limit, 50)},
        )
        out = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            out.append(LeaderboardEntry(
                address=(r.get("proxyWallet") or "").lower(),
                username=r.get("userName") or "",
                rank=int(safe_float(r.get("rank"), 0)),
                pnl=safe_float(r.get("pnl")),
                volume=safe_float(r.get("vol")),
                window=window,
                category=category,
            ))
        return [e for e in out if e.address]

    def discover_wallets(self, windows=LEADERBOARD_WINDOWS,
                         categories=LEADERBOARD_CATEGORIES,
                         order_bys=("PNL", "VOL")) -> dict[str, LeaderboardEntry]:
        """Sweep the leaderboard across every window x category x metric.

        Ranking by VOL as well as PNL is deliberate: it surfaces the
        high-turnover wallets so the market-maker screen can EXCLUDE them, rather
        than letting them leak in through a category leaderboard later.
        """
        found: dict[str, LeaderboardEntry] = {}
        for window in windows:
            for category in categories:
                for order_by in order_bys:
                    try:
                        for entry in self.leaderboard(window, category, order_by):
                            prev = found.get(entry.address)
                            if prev is None or entry.pnl > prev.pnl:
                                found[entry.address] = entry
                    except Exception as e:
                        log.warning("leaderboard %s/%s/%s failed: %s",
                                    window, category, order_by, e)
        return found

    def positions(self, address: str, limit: int = 500) -> list[WalletPosition]:
        rows = request_json(f"{DATA}/positions", limiter=self.data_limiter,
                            params={"user": address, "limit": limit})
        out = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            out.append(WalletPosition(
                address=address.lower(),
                market_id=str(r.get("conditionId") or ""),
                title=r.get("title") or "",
                side=Side.YES if int(safe_float(r.get("outcomeIndex"), 0)) == 0
                else Side.NO,
                size=safe_float(r.get("size")),
                avg_price=safe_float(r.get("avgPrice")),
                current_price=safe_float(r.get("curPrice")),
                realized_pnl=safe_float(r.get("realizedPnl")),
                cash_pnl=safe_float(r.get("cashPnl")),
                redeemable=bool(r.get("redeemable")),
            ))
        return out

    def closed_positions(self, address: str, limit: int = 300
                         ) -> list[WalletPosition]:
        """Resolved trading history - the sample the skill screen is built on.

        `/positions` returns only CURRENT holdings (median 3 per wallet), so
        scoring against it can never accumulate the 30+ resolved outcomes needed
        to distinguish skill from noise. Resolved history lives here.

        CRITICAL: `sortBy=TIMESTAMP` is not optional. The endpoint defaults to
        sorting by realizedPnl DESCENDING and caps every response at 50 rows, so
        the default sample is a wallet's fifty BEST trades - measured live at
        50/50 winners, which scored every wallet at an impossible +0.5 to +0.75
        entry-adjusted edge. Chronological order gives ~21/50 winners: a real
        distribution. Sampling a trader's best trades to judge their skill is the
        exact survivorship bias this whole screen exists to defeat.
        """
        out: list[WalletPosition] = []
        page_size = 50  # server-enforced cap regardless of `limit`
        for offset in range(0, max(limit, page_size), page_size):
            rows = request_json(
                f"{DATA}/closed-positions", limiter=self.data_limiter,
                params={"user": address, "limit": page_size, "offset": offset,
                        "sortBy": "TIMESTAMP"},
            )
            if not rows:
                break
            out.extend(self._parse_closed(address, rows))
            if len(rows) < page_size:
                break
        return out

    @staticmethod
    def _parse_closed(address: str, rows: list) -> list[WalletPosition]:
        out = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            avg = safe_float(r.get("avgPrice"))
            if not (0.0 < avg < 1.0):
                continue
            out.append(WalletPosition(
                address=address.lower(),
                market_id=str(r.get("conditionId") or ""),
                title=r.get("title") or "",
                side=Side.YES if int(safe_float(r.get("outcomeIndex"), 0)) == 0
                else Side.NO,
                size=safe_float(r.get("size")),
                avg_price=avg,
                current_price=safe_float(r.get("curPrice")),
                realized_pnl=safe_float(r.get("realizedPnl")),
                cash_pnl=safe_float(r.get("realizedPnl")),
                redeemable=True,   # by definition: this position is closed
            ))
        return out

    def activity(self, address: str, limit: int = 500) -> list[WalletActivity]:
        rows = request_json(f"{DATA}/activity", limiter=self.data_limiter,
                            params={"user": address, "limit": limit})
        out = []
        for r in (rows or []):
            if not isinstance(r, dict) or r.get("type") != "TRADE":
                continue
            out.append(WalletActivity(
                address=address.lower(),
                ts=_ts(r.get("timestamp")) or datetime.now(timezone.utc),
                market_id=str(r.get("conditionId") or ""),
                title=r.get("title") or "",
                side=str(r.get("side") or ""),
                action=str(r.get("type") or ""),
                size=safe_float(r.get("size")),
                usdc_size=safe_float(r.get("usdcSize")),
                price=safe_float(r.get("price")),
            ))
        return out
