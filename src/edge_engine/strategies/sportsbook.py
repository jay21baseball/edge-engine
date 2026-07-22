"""Sportsbook divergence: sharp lines vs Polymarket sports prices.

Books move on injury and lineup news within seconds. Polymarket, priced by
retail flow, takes minutes to hours. Where the devigged sharp probability and
the Polymarket ask disagree by more than fees plus a safety margin, the market
is stale.

Single leg, one venue, no leg risk - which is why this works at $2,500 when
cross-venue arbitrage does not.

The two ways this goes wrong, both guarded below:
  1. Comparing raw book odds without removing the vig, which manufactures ~2-5
     points of phantom edge on every market. Handled in odds_api.devig.
  2. Matching the wrong game. A confident price on a market you misidentified is
     worse than no signal, so matching requires BOTH team names and a start time
     inside a tight window.
"""
from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from ..ingest.models import Event, Market, Side, Venue
from ..ingest.odds_api import OddsEvent, consensus_probability, now_utc
from ..sizing.fees import effective_rate
from .base import Signal, Strategy

log = logging.getLogger(__name__)

MAX_START_SKEW_HOURS = 6.0
MIN_EDGE_OVER_FEES = 0.02

# Pure affixes only. `city`, `united`, `rovers` and similar are NOT noise -
# stripping them collapsed "Manchester United" and "Manchester City" to the same
# token and matched two different fixtures, which is how you get a confident
# signal on the wrong game.
_NOISE = re.compile(r"\b(fc|cf|sc|ac|afc|cd|club|de|da|do|the|of)\b|[^a-z0-9 ]")

_ALIASES = {"utd": "united", "man": "manchester", "psg": "parissaintgermain"}

# Gender markers are hard discriminators. An 'Arsenal Women' fixture is a
# different event from 'Arsenal', and token-subset logic alone would merge them.
_WOMEN = re.compile(r"\b(women|womens|ladies|fem|feminino|wfc|w)\b")


def normalize_team(name: str) -> str:
    cleaned = _NOISE.sub(" ", (name or "").lower())
    return " ".join(_ALIASES.get(t, t) for t in cleaned.split())


def is_womens(name: str) -> bool:
    return bool(_WOMEN.search((name or "").lower()))


def team_tokens(name: str) -> set[str]:
    stripped = _WOMEN.sub(" ", normalize_team(name))
    return {t for t in stripped.split() if len(t) > 2}


def teams_match(a: str, b: str) -> bool:
    """True when two names plausibly refer to the same team.

    Requires the distinctive token sets to be EQUAL after affix stripping and
    alias resolution. Two weaker rules were tried and both produced wrong
    matches on live-looking data:

      overlap  -> 'Manchester United' matched 'Manchester City' on the shared
                  city name.
      subset   -> 'AC Milan' reduces to {milan}, a subset of {inter, milan}, so
                  it matched Inter Milan.

    Equality costs some legitimate matches ('Tottenham' will not match
    'Tottenham Hotspur'). That is the correct direction to fail: a missed match
    costs one signal, a wrong match costs a bet placed on a game you were never
    looking at.
    """
    if is_womens(a) != is_womens(b):
        return False
    ta, tb = team_tokens(a), team_tokens(b)
    if not ta or not tb:
        return False
    return ta == tb


class SportsbookDivergence(Strategy):
    name = "sportsbook_divergence"
    min_bankroll = 0.0

    def __init__(self, min_edge: float = MIN_EDGE_OVER_FEES,
                 max_start_skew_hours: float = MAX_START_SKEW_HOURS,
                 require_pinnacle: bool = False):
        self.min_edge = min_edge
        self.max_skew = max_start_skew_hours
        self.require_pinnacle = require_pinnacle

    # ------------------------------------------------------------- matching

    def match(self, market: Market, event: Event,
              odds_events: Iterable[OddsEvent]) -> Optional[tuple[OddsEvent, str]]:
        """Pair a Polymarket market with a book event and identify which side.

        Returns (odds_event, team_the_YES_side_backs) or None.
        """
        if not market.close_ts:
            return None
        title = f"{event.title} {market.title}"

        for odds_event in odds_events:
            skew = abs((odds_event.commence_time - market.close_ts)
                       .total_seconds()) / 3600.0
            if skew > self.max_skew:
                continue
            home_in = self._mentions(title, odds_event.home_team)
            away_in = self._mentions(title, odds_event.away_team)
            # BOTH teams must appear, or we cannot be sure which game this is.
            if not (home_in and away_in):
                continue
            backed = self._backed_team(market.title, odds_event)
            if backed:
                return odds_event, backed
        return None

    @staticmethod
    def _mentions(title: str, team: str) -> bool:
        """Does `title` name this team? Every distinctive token must be present.

        Requiring ALL tokens rather than any is what keeps 'Manchester City' from
        matching a Manchester United fixture title.
        """
        tokens = team_tokens(team)
        if not tokens:
            return False
        haystack = set(normalize_team(title).split())
        return tokens <= haystack

    @staticmethod
    def _backed_team(market_title: str, odds_event: OddsEvent) -> Optional[str]:
        """Which team does a YES on this market back?

        Polymarket sub-markets in a game event are usually the team name itself
        ('CF Cruz Azul'), so the sub-title identifies the side directly. A market
        naming both teams equally (a Draw or an over/under leg) is ambiguous and
        is skipped rather than guessed.
        """
        home_hit = SportsbookDivergence._mentions(market_title,
                                                  odds_event.home_team)
        away_hit = SportsbookDivergence._mentions(market_title,
                                                  odds_event.away_team)
        if home_hit and not away_hit:
            return odds_event.home_team
        if away_hit and not home_hit:
            return odds_event.away_team
        return None

    # ------------------------------------------------------------------ scan

    def evaluate(self, market: Market, event: Event, odds_event: OddsEvent,
                 team: str) -> Optional[Signal]:
        priced = consensus_probability(odds_event, team)
        if priced is None:
            return None
        fair, books_used = priced
        if self.require_pinnacle and books_used != 1:
            return None
        if not (0.02 < fair < 0.98):
            return None

        ask = market.yes_ask
        if ask is None or not (0.0 < ask < 1.0):
            return None

        rate = effective_rate(Venue.POLYMARKET, event.category, market.fee_rate)
        fee_fraction = (rate * ask * (1.0 - ask)) / ask if ask > 0 else 0.0
        edge = (fair - ask) / ask - fee_fraction
        if edge < self.min_edge:
            return None

        hours_to_start = max(
            (odds_event.commence_time - now_utc()).total_seconds() / 3600.0, 0.0
        )
        # A wider gap is more likely to be a mismatch than a gift. Confidence
        # falls away as the divergence becomes implausibly large.
        raw_gap = fair - ask
        confidence = min(1.0, books_used * 0.4 + 0.4) * (
            1.0 if raw_gap < 0.12 else max(0.2, 0.12 / raw_gap)
        )

        return Signal(
            strategy=self.name,
            venue=Venue.POLYMARKET,
            market_id=market.market_id,
            title=market.title or event.title,
            side=Side.YES.value,
            entry_price=ask,
            est_probability=fair,
            edge=edge,
            confidence=confidence,
            days_to_resolution=max(hours_to_start / 24.0, 1.0 / 24.0),
            category=event.category or "sports",
            deterministic=False,
            rationale={
                "sharp_source": "pinnacle" if books_used == 1 else
                                f"consensus of {books_used} sharp books",
                "devigged_probability": round(fair, 4),
                "polymarket_ask": round(ask, 4),
                "raw_gap_points": round(raw_gap * 100, 2),
                "fee_drag_pct": round(fee_fraction * 100, 2),
                "net_edge_pct": round(edge * 100, 2),
                "hours_to_start": round(hours_to_start, 1),
                "matched_game": f"{odds_event.away_team} @ {odds_event.home_team}",
                "backing": team,
            },
            counter_case=(
                f"Assumes the sharp line is right and Polymarket is stale. If the "
                f"book has not yet moved on news Polymarket already priced, the "
                f"gap is real information and you are on the wrong side. Verify "
                f"the matched game is correct: "
                f"{odds_event.away_team} @ {odds_event.home_team}."
            ),
        )

    def scan(self, context) -> list[Signal]:
        odds_events = getattr(context, "odds_events", None) or []
        if not odds_events:
            return []

        signals: list[Signal] = []
        for event in context.events:
            if event.venue is not Venue.POLYMARKET:
                continue
            if "sport" not in (event.category or "").lower():
                continue
            for market in event.markets:
                if market.status != "open":
                    continue
                matched = self.match(market, event, odds_events)
                if not matched:
                    continue
                odds_event, team = matched
                try:
                    signal = self.evaluate(market, event, odds_event, team)
                except Exception as e:
                    log.debug("evaluate failed for %s: %s", market.market_id, e)
                    continue
                if signal:
                    signals.append(signal)
        log.info("sportsbook divergence: %d signals from %d book events",
                 len(signals), len(odds_events))
        return sorted(signals, key=lambda s: -s.score)
