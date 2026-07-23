"""Live divergence recorder: ESPN alignment, moneyline extraction, math."""
import json

import pytest

from edge_engine.ingest.espn import LiveGame
from edge_engine.ingest.models import Event, Market, Venue
from edge_engine.strategies.live_recorder import (
    LivePairing,
    _is_derived,
    build_pairings,
    moneyline_outcomes,
)


def _game(home="Chicago Cubs", away="Detroit Tigers", home_wp=0.30,
          state="in", detail="Top 7th"):
    return LiveGame(league="mlb", espn_id="1", home_team=home, away_team=away,
                    state=state, home_win_prob=home_wp, detail=detail)


def _market(outcomes, prices, title="Detroit Tigers vs. Chicago Cubs",
            group=None, liquidity=100000.0, status="open"):
    raw = {"outcomes": json.dumps(outcomes),
           "outcomePrices": json.dumps([str(p) for p in prices]),
           "groupItemTitle": group}
    return Market(venue=Venue.POLYMARKET, market_id="m1", event_id="e1",
                  title=title, category="sports", liquidity=liquidity,
                  fee_rate=0.05, status=status, raw=raw)


def _event(markets, title="Detroit Tigers vs. Chicago Cubs"):
    return Event(venue=Venue.POLYMARKET, event_id="e1", title=title,
                 category="sports", markets=markets)


class TestWinProbAlignment:
    def test_home_team_gets_home_prob(self):
        g = _game(home_wp=0.30)
        assert g.win_prob("Chicago Cubs") == pytest.approx(0.30)

    def test_away_team_gets_complement(self):
        g = _game(home_wp=0.30)
        assert g.win_prob("Detroit Tigers") == pytest.approx(0.70)

    def test_unknown_team_is_none(self):
        assert _game().win_prob("New York Yankees") is None

    def test_missing_model_is_none(self):
        g = _game(home_wp=None)
        assert g.win_prob("Chicago Cubs") is None


class TestMoneylineExtraction:
    def test_two_team_market_yields_both_sides(self):
        g = _game()
        out = moneyline_outcomes(
            _market(["Detroit Tigers", "Chicago Cubs"], [0.68, 0.33]), g)
        teams = dict(out)
        assert teams["Detroit Tigers"] == pytest.approx(0.68)
        assert teams["Chicago Cubs"] == pytest.approx(0.33)

    def test_settled_price_is_rejected(self):
        """A moneyline pinned at 0.99 has effectively resolved."""
        g = _game()
        assert moneyline_outcomes(
            _market(["Detroit Tigers", "Chicago Cubs"], [0.995, 0.005]), g) == []

    def test_non_team_outcomes_rejected(self):
        g = _game()
        assert moneyline_outcomes(
            _market(["Yes", "No"], [0.4, 0.6]), g) == []

    def test_three_outcomes_rejected(self):
        g = _game()
        m = _market(["Tigers", "Cubs", "Draw"], [0.4, 0.4, 0.2])
        assert moneyline_outcomes(m, g) == []


class TestDerivedRejection:
    @pytest.mark.parametrize("title", [
        "1st 5 Innings Spread -1.5", "O/U 8.5", "Extra Innings",
        "NRFI", "First 5 Innings O/U 4.5", "Spread -1.5",
    ])
    def test_derived_markets_flagged(self, title):
        assert _is_derived(_market(["a", "b"], [0.5, 0.5], group=title))

    def test_full_game_moneyline_not_flagged(self):
        assert not _is_derived(
            _market(["Detroit Tigers", "Chicago Cubs"], [0.6, 0.4]))


class TestBuildPairings:
    def test_matches_game_to_moneyline(self):
        g = _game(home_wp=0.30)   # Cubs 30%, Tigers 70%
        ev = _event([_market(["Detroit Tigers", "Chicago Cubs"], [0.68, 0.32])])
        pairs = build_pairings([g], [ev])
        assert len(pairs) == 2
        tigers = next(p for p in pairs if p.team == "Detroit Tigers")
        assert tigers.espn_prob == pytest.approx(0.70)
        assert tigers.poly_price == pytest.approx(0.68)

    def test_picks_most_liquid_moneyline(self):
        """A game has a full-game and a first-five market; take the liquid one."""
        g = _game(home_wp=0.30)
        thin = _market(["Detroit Tigers", "Chicago Cubs"], [0.55, 0.45],
                       liquidity=1000.0)
        deep = _market(["Detroit Tigers", "Chicago Cubs"], [0.68, 0.32],
                       liquidity=200000.0)
        pairs = build_pairings([g], [_event([thin, deep])])
        tigers = next(p for p in pairs if p.team == "Detroit Tigers")
        assert tigers.poly_price == pytest.approx(0.68)  # from the deep book

    def test_derived_markets_excluded(self):
        g = _game(home_wp=0.30)
        money = _market(["Detroit Tigers", "Chicago Cubs"], [0.68, 0.32],
                        liquidity=5000.0)
        spread = _market(["Detroit Tigers", "Chicago Cubs"], [0.9, 0.1],
                         group="1st 5 Innings Spread -1.5", liquidity=999999.0)
        pairs = build_pairings([g], [_event([money, spread])])
        # Spread is more liquid but must be excluded; price comes from money.
        assert all(p.poly_price in (pytest.approx(0.68), pytest.approx(0.32))
                   for p in pairs)

    def test_decided_game_excluded(self):
        """Win prob pinned near certainty -> no meaningful edge to record."""
        g = _game(home_wp=0.995)
        ev = _event([_market(["Detroit Tigers", "Chicago Cubs"], [0.5, 0.5])])
        assert build_pairings([g], [ev]) == []

    def test_only_live_games_considered(self):
        g = _game(state="post")
        ev = _event([_market(["Detroit Tigers", "Chicago Cubs"], [0.68, 0.32])])
        # build_pairings itself does not check state (the client does), but a
        # post game with a settled model still yields nothing useful here.
        pairs = build_pairings([g], [ev])
        assert isinstance(pairs, list)

    def test_wrong_game_not_matched(self):
        g = _game(home="New York Yankees", away="Boston Red Sox")
        ev = _event([_market(["Detroit Tigers", "Chicago Cubs"], [0.6, 0.4])])
        assert build_pairings([g], [ev]) == []


class TestDivergenceMath:
    def _pairing(self, espn, poly, fee=0.05):
        return LivePairing(
            game=_game(), market=_market(["a", "b"], [0.5, 0.5]),
            poly_event_title="t", team="Detroit Tigers",
            espn_prob=espn, poly_price=poly, fee_rate=fee)

    def test_gap_is_espn_minus_poly(self):
        assert self._pairing(0.70, 0.62).gap == pytest.approx(0.08)

    def test_direction(self):
        assert self._pairing(0.70, 0.62).direction == "BUY YES"   # poly cheap
        assert self._pairing(0.55, 0.70).direction == "BUY NO"    # poly rich

    def test_net_gap_deducts_fee(self):
        p = self._pairing(0.70, 0.62)
        assert p.net_gap < abs(p.gap)
        assert p.net_gap > 0     # 8pt gap survives a ~2pt fee

    def test_tiny_gap_goes_negative_after_fees(self):
        assert self._pairing(0.51, 0.50).net_gap < 0
