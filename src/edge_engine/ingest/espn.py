"""ESPN free live-game feed — the sharp reference for the live recorder.

ESPN publishes live win probability for major US sports with no API key and no
cost. For a game in progress it updates per play (per pitch in MLB), which makes
it a genuine model-based reference to measure a lagging Polymarket price
against — closer to a sharp line than another retail market could ever be.

Endpoints (unofficial but stable and widely used):
    scoreboard: .../sports/{sport}/{league}/scoreboard          one call/league
    summary:    .../sports/{sport}/{league}/summary?event={id}  one call/game

Soccer has no win-probability model on ESPN, so the live recorder covers the
sports that do: MLB, WNBA, NBA, NFL.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from .http import RateLimiter, request_json, safe_float

log = logging.getLogger(__name__)

SITE = "https://site.api.espn.com/apis/site/v2/sports"

# league key -> (espn sport path, espn league path). Only sports with a live
# win-probability model belong here.
LEAGUES: dict[str, tuple[str, str]] = {
    "mlb": ("baseball", "mlb"),
    "wnba": ("basketball", "wnba"),
    "nba": ("basketball", "nba"),
    "nfl": ("football", "nfl"),
    "cfb": ("football", "college-football"),
}


@dataclass
class LiveGame:
    league: str
    espn_id: str
    home_team: str
    away_team: str
    state: str                      # "pre" | "in" | "post"
    home_win_prob: Optional[float] = None   # 0..1, ESPN's live model
    detail: str = ""                # e.g. "Bot 7th", "Q3 4:12"
    home_score: int = 0
    away_score: int = 0

    @property
    def is_live(self) -> bool:
        return self.state == "in"

    def win_prob(self, team: str) -> Optional[float]:
        """ESPN win probability for a named team, or None if unmatched."""
        from ..strategies.sportsbook import teams_match
        if self.home_win_prob is None:
            return None
        if teams_match(team, self.home_team):
            return self.home_win_prob
        if teams_match(team, self.away_team):
            return 1.0 - self.home_win_prob
        return None


class EspnClient:
    def __init__(self):
        # ESPN is public and generous, but stay polite.
        self.limiter = RateLimiter(4.0, burst=8)
        self.headers = {"User-Agent": "Mozilla/5.0"}

    def _get(self, url: str):
        return request_json(url, limiter=self.limiter)

    def scoreboard(self, league: str) -> list[LiveGame]:
        sport, lg = LEAGUES.get(league, (None, None))
        if sport is None:
            return []
        try:
            data = self._get(f"{SITE}/{sport}/{lg}/scoreboard")
        except Exception as e:
            log.warning("espn scoreboard %s failed: %s", league, e)
            return []

        games: list[LiveGame] = []
        for ev in (data.get("events") or []):
            comp = (ev.get("competitions") or [{}])[0]
            competitors = comp.get("competitors") or []
            home = next((c for c in competitors
                         if c.get("homeAway") == "home"), None)
            away = next((c for c in competitors
                         if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            status = (ev.get("status") or {}).get("type") or {}
            games.append(LiveGame(
                league=league,
                espn_id=str(ev.get("id") or ""),
                home_team=(home.get("team") or {}).get("displayName", ""),
                away_team=(away.get("team") or {}).get("displayName", ""),
                state=status.get("state", "pre"),
                detail=status.get("shortDetail", ""),
                home_score=int(safe_float(home.get("score"))),
                away_score=int(safe_float(away.get("score"))),
            ))
        return games

    def win_probability(self, league: str, espn_id: str) -> Optional[float]:
        """Latest home win probability for a live game (0..1)."""
        sport, lg = LEAGUES.get(league, (None, None))
        if sport is None:
            return None
        try:
            data = self._get(f"{SITE}/{sport}/{lg}/summary?event={espn_id}")
        except Exception as e:
            log.debug("espn summary %s/%s failed: %s", league, espn_id, e)
            return None
        wp = data.get("winprobability") or data.get("winProbability") or []
        if not wp:
            return None
        latest = wp[-1]
        home = latest.get("homeWinPercentage")
        return safe_float(home) if home is not None else None

    def live_games(self, leagues: list[str]) -> list[LiveGame]:
        """Every in-progress game across the requested leagues, win-prob filled."""
        out: list[LiveGame] = []
        for league in leagues:
            for game in self.scoreboard(league):
                if not game.is_live:
                    continue
                game.home_win_prob = self.win_probability(league, game.espn_id)
                if game.home_win_prob is not None:
                    out.append(game)
        return out
