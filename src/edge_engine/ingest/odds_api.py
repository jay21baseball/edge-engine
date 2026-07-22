"""Sharp sportsbook lines via The Odds API (free tier: 500 requests/month).

Polymarket's sports liquidity is retail; sportsbooks are priced by professionals
and move on injury or lineup news within seconds while Polymarket takes minutes
to hours. That lag is the edge, and unlike cross-venue arb you execute only ONE
leg on one venue - so there is no leg risk and it works at a small bankroll.

Get a free key at https://the-odds-api.com and set ODDS_API_KEY.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .http import RateLimiter, request_json, safe_float

log = logging.getLogger(__name__)

BASE = "https://api.the-odds-api.com/v4"

# Books that price sharply. Pinnacle is the reference; the others are used only
# to form a consensus when Pinnacle has not posted a line.
SHARP_BOOKS = ("pinnacle", "betfair_ex_eu", "betfair_ex_uk", "smarkets",
               "matchbook", "betonlineag", "lowvig")

DEFAULT_SPORTS = (
    "basketball_nba", "americanfootball_nfl", "baseball_mlb", "icehockey_nhl",
    "soccer_epl", "soccer_uefa_champs_league", "mma_mixed_martial_arts",
    "tennis_atp_aus_open_singles",
)


@dataclass
class BookLine:
    book: str
    outcomes: dict[str, float] = field(default_factory=dict)  # team -> decimal odds

    @property
    def is_complete(self) -> bool:
        return len(self.outcomes) >= 2


@dataclass
class OddsEvent:
    event_id: str
    sport: str
    home_team: str
    away_team: str
    commence_time: datetime
    books: list[BookLine] = field(default_factory=list)

    @property
    def teams(self) -> tuple[str, str]:
        return self.home_team, self.away_team


def devig_multiplicative(decimal_odds: dict[str, float]) -> dict[str, float]:
    """Strip the bookmaker margin to recover true implied probabilities.

    Raw implied probability is 1/decimal_odds, and those sum to MORE than 1 -
    the excess is the book's margin (the 'vig' or 'overround'). A 2-way market
    at -110/-110 implies 52.4% + 52.4% = 104.8%.

    Comparing raw implied probabilities against a Polymarket price manufactures
    roughly 2-5 points of phantom edge on EVERY market, which would flood you
    with signals that are all margin and no edge. Normalizing by the overround
    removes it:

        p_true(i) = (1/odds_i) / sum_j(1/odds_j)

    Multiplicative devigging is the standard approach and assumes margin is
    applied proportionally. It is slightly biased on heavy favourites, which is
    acceptable here - the alternative methods need more data than the free tier
    provides, and this errs toward UNDERSTATING edge on longshots.
    """
    raw = {k: 1.0 / v for k, v in decimal_odds.items() if v and v > 1.0}
    total = sum(raw.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in raw.items()}


def consensus_probability(event: OddsEvent, team: str) -> Optional[tuple[float, int]]:
    """Devigged probability for `team`, preferring Pinnacle, else sharp consensus.

    Returns (probability, books_used) or None.
    """
    priced: list[float] = []
    for line in event.books:
        if not line.is_complete:
            continue
        fair = devig_multiplicative(line.outcomes)
        if team in fair:
            if line.book == "pinnacle":
                return fair[team], 1     # Pinnacle alone beats a consensus
            priced.append(fair[team])
    if not priced:
        return None
    return sum(priced) / len(priced), len(priced)


class OddsApiClient:
    def __init__(self, api_key: Optional[str]):
        self.api_key = api_key
        self.limiter = RateLimiter(2.0, burst=4)
        self.requests_remaining: Optional[int] = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    def odds(self, sport: str, regions: str = "us,eu",
             markets: str = "h2h") -> list[OddsEvent]:
        """Fetch head-to-head odds for one sport.

        One request per sport, so the free tier's 500/month budget allows roughly
        2 sports scanned hourly, or all 8 a few times a day. Prefer fewer sports
        scanned more often over many scanned rarely - the edge is a timing lag,
        so a stale line is worth nothing.
        """
        if not self.enabled:
            return []
        rows = request_json(
            f"{BASE}/sports/{sport}/odds", limiter=self.limiter,
            params={"apiKey": self.api_key, "regions": regions,
                    "markets": markets, "oddsFormat": "decimal"},
        )
        out: list[OddsEvent] = []
        for r in (rows or []):
            if not isinstance(r, dict):
                continue
            try:
                commence = datetime.fromisoformat(
                    str(r.get("commence_time", "")).replace("Z", "+00:00")
                )
            except ValueError:
                continue
            books = []
            for b in (r.get("bookmakers") or []):
                key = (b.get("key") or "").lower()
                if key not in SHARP_BOOKS:
                    continue
                for market in (b.get("markets") or []):
                    if market.get("key") != "h2h":
                        continue
                    outcomes = {
                        str(o.get("name")): safe_float(o.get("price"))
                        for o in (market.get("outcomes") or [])
                        if o.get("name") and safe_float(o.get("price")) > 1.0
                    }
                    if len(outcomes) >= 2:
                        books.append(BookLine(book=key, outcomes=outcomes))
            if books:
                out.append(OddsEvent(
                    event_id=str(r.get("id", "")), sport=sport,
                    home_team=str(r.get("home_team", "")),
                    away_team=str(r.get("away_team", "")),
                    commence_time=commence, books=books,
                ))
        log.info("odds: %s -> %d events with sharp lines", sport, len(out))
        return out

    def list_sports(self, all_sports: bool = False) -> list[dict]:
        """Every sport key the API offers. Free - does not consume quota."""
        if not self.enabled:
            return []
        rows = request_json(
            f"{BASE}/sports", limiter=self.limiter,
            params={"apiKey": self.api_key,
                    "all": "true" if all_sports else None},
        )
        return [r for r in (rows or []) if isinstance(r, dict)]

    def fetch_all(self, sports=DEFAULT_SPORTS) -> list[OddsEvent]:
        events: list[OddsEvent] = []
        for sport in sports:
            try:
                events.extend(self.odds(sport))
            except Exception as e:
                log.warning("odds fetch failed for %s: %s", sport, e)
        return events


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
