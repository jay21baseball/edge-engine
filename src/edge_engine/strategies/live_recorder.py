"""Live divergence recorder: ESPN win probability vs Polymarket price.

This is a RECORDER, not an alerter. It observes and logs, exactly as
cross-venue arbitrage does below its bankroll gate. The question it exists to
answer, at 30-60 second resolution and for free:

    When a game swings and ESPN's live model repositions, how far behind does
    the Polymarket price lag, how large is the gap, and how long does it stay
    open before retail catches up?

Only after that data exists is it honest to build an alerter on top. A live
scanner that fires on a gap nobody could have filled in time is worse than no
scanner - it manufactures confidence in an edge that was never reachable.

Matching: an ESPN game is paired to a Polymarket market when both teams appear
in the Polymarket event title and the market backs one specific team (by its
groupItemTitle). The ESPN win probability is then aligned to that team's side.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from ..ingest.espn import LiveGame
from ..ingest.http import parse_json_field, safe_float
from ..ingest.models import Event, Market, Venue
from ..sizing.fees import effective_rate
from .sportsbook import team_tokens, teams_match

log = logging.getLogger(__name__)

MIN_GAP_TO_LOG = 0.01     # 1 point — below this it is noise, not a signal


@dataclass
class LivePairing:
    """One ESPN game matched to one Polymarket team market."""

    game: LiveGame
    market: Market
    poly_event_title: str
    team: str                        # the team this market's YES backs
    espn_prob: float                 # ESPN win prob for `team` (0..1)
    poly_price: float                # Polymarket ask for `team` YES (0..1)
    fee_rate: float

    @property
    def gap(self) -> float:
        """Raw divergence in probability, ESPN minus Polymarket.

        Positive means ESPN rates the team likelier than Polymarket prices it —
        i.e. Polymarket looks cheap on that side if ESPN is right and leading.
        """
        return self.espn_prob - self.poly_price

    @property
    def fee_drag(self) -> float:
        p = self.poly_price
        return effective_rate(Venue.POLYMARKET, "sports", self.fee_rate) * (1 - p)

    @property
    def net_gap(self) -> float:
        """Gap after the Polymarket taker fee on the side you would buy."""
        return abs(self.gap) - self.fee_drag

    @property
    def direction(self) -> str:
        return "BUY YES" if self.gap > 0 else "BUY NO"


def moneyline_outcomes(market: Market, game: LiveGame) -> list[tuple]:
    """Extract (team, price) for a two-team moneyline market.

    Polymarket frames a game's moneyline as ONE market whose two outcomes are
    the team names, with a price per outcome - not one market per team. Derived
    markets (spread, over/under, NRFI, extra innings) have non-team outcomes and
    are skipped: there is no single team to align an ESPN win probability to.
    """
    outcomes = [str(o) for o in parse_json_field(market.raw.get("outcomes"))]
    prices = [safe_float(p)
              for p in parse_json_field(market.raw.get("outcomePrices"))]
    if len(outcomes) != 2 or len(prices) != 2:
        return []
    # Both outcomes must name the two teams in this game, or it is not the
    # moneyline (e.g. Yes/No derived markets, or a different matchup).
    teams = (game.home_team, game.away_team)
    aligned = []
    for name, price in zip(outcomes, prices):
        matched = next((t for t in teams if teams_match(name, t)), None)
        # Reject settled prices: a live moneyline pinned at 0.98+ is a market
        # that has effectively already resolved, not a divergence.
        if matched is None or not (0.02 < price < 0.98):
            return []
        aligned.append((matched, price))
    # A game hosts several two-team markets (full game, first-five-innings,
    # winner). Only the primary full-game moneyline is comparable to an ESPN
    # game win probability - identify it by liquidity elsewhere.
    return aligned


def _is_derived(market: Market) -> bool:
    """True for sub-markets that are not the full-game moneyline."""
    text = f"{market.raw.get('groupItemTitle') or ''} {market.title}".lower()
    return any(word in text for word in (
        "1st 5", "first 5", "innings", "half", "quarter", "period",
        "spread", "o/u", "over", "under", "nrfi", "extra"))


def match_games(games: list[LiveGame], events: list[Event]) -> list[tuple]:
    """Pair live ESPN games with Polymarket moneyline outcomes.

    Returns (game, event, market, team, price) tuples - one per team side of a
    matched moneyline. A pairing requires both game teams to appear in the
    Polymarket event title (right game) and a two-team moneyline market inside.
    """
    pairs = []
    for game in games:
        # Gather every candidate moneyline for this game across all its events,
        # then keep only the most liquid one - that is the primary full-game
        # market, and the only one comparable to an ESPN game win probability.
        candidates = []
        for event in events:
            etitle = event.title or ""
            if not (_mentions(etitle, game.home_team)
                    and _mentions(etitle, game.away_team)):
                continue
            for market in event.markets:
                if market.status != "open" or _is_derived(market):
                    continue
                aligned = moneyline_outcomes(market, game)
                if aligned:
                    candidates.append((event, market, aligned))
        if not candidates:
            continue
        event, market, aligned = max(candidates, key=lambda c: c[1].liquidity)
        for team, price in aligned:
            pairs.append((game, event, market, team, price))
    return pairs


def _mentions(title: str, team: str) -> bool:
    tokens = team_tokens(team)
    return bool(tokens) and tokens <= (
        set(team_tokens(title)) | _title_words(title))


def _title_words(title: str) -> set:
    from .sportsbook import normalize_team
    return set(normalize_team(title).split())


def build_pairings(games: list[LiveGame], events: list[Event]) -> list[LivePairing]:
    """Full divergence records for every matched, priceable live pairing."""
    out: list[LivePairing] = []
    for game, event, market, team, price in match_games(games, events):
        espn = game.win_prob(team)
        # Skip games whose model has pinned to a near-certain outcome - the
        # remaining edge is illusory and the game is effectively decided.
        if espn is None or not (0.02 < espn < 0.98):
            continue
        out.append(LivePairing(
            game=game, market=market, poly_event_title=event.title,
            team=team, espn_prob=espn, poly_price=price,
            fee_rate=market.fee_rate if market.fee_rate is not None else 0.05,
        ))
    return out
