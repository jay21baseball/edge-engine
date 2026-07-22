"""Devigging and match safety for the sportsbook divergence strategy."""
from datetime import datetime, timedelta, timezone

import pytest

from edge_engine.ingest.models import Event, Market, Venue
from edge_engine.ingest.odds_api import (
    BookLine,
    OddsEvent,
    consensus_probability,
    devig_multiplicative,
)
from edge_engine.strategies.sportsbook import SportsbookDivergence, teams_match

NOW = datetime.now(timezone.utc)
SOON = NOW + timedelta(hours=3)


class TestDevig:
    def test_removes_the_overround(self):
        """-110/-110 is 1.909 decimal each: raw implies 104.8%, true is 50/50."""
        fair = devig_multiplicative({"A": 1.909, "B": 1.909})
        assert fair["A"] == pytest.approx(0.5, abs=1e-3)
        assert sum(fair.values()) == pytest.approx(1.0)

    def test_raw_implied_would_overstate_edge(self):
        raw_sum = 1 / 1.909 + 1 / 1.909
        assert raw_sum > 1.04           # ~4.8 points of pure margin
        fair = devig_multiplicative({"A": 1.909, "B": 1.909})
        assert fair["A"] < 1 / 1.909    # devigging always reduces the favourite

    def test_asymmetric_market(self):
        fair = devig_multiplicative({"Fav": 1.25, "Dog": 3.75})
        assert sum(fair.values()) == pytest.approx(1.0)
        assert fair["Fav"] == pytest.approx(0.75, abs=0.01)

    def test_ignores_degenerate_odds(self):
        assert devig_multiplicative({"A": 0.0, "B": 1.0}) == {}


class TestConsensus:
    def _event(self, books):
        return OddsEvent("e1", "soccer", "Home FC", "Away FC", SOON, books)

    def test_pinnacle_wins_outright(self):
        event = self._event([
            BookLine("smarkets", {"Home FC": 3.0, "Away FC": 1.5}),
            BookLine("pinnacle", {"Home FC": 2.0, "Away FC": 2.0}),
        ])
        prob, used = consensus_probability(event, "Home FC")
        assert used == 1
        assert prob == pytest.approx(0.5, abs=1e-3)

    def test_falls_back_to_consensus(self):
        event = self._event([
            BookLine("smarkets", {"Home FC": 2.0, "Away FC": 2.0}),
            BookLine("matchbook", {"Home FC": 2.0, "Away FC": 2.0}),
        ])
        prob, used = consensus_probability(event, "Home FC")
        assert used == 2 and prob == pytest.approx(0.5, abs=1e-3)

    def test_none_when_team_absent(self):
        event = self._event([BookLine("pinnacle", {"X": 2.0, "Y": 2.0})])
        assert consensus_probability(event, "Home FC") is None


class TestTeamMatching:
    def test_matches_across_naming_conventions(self):
        assert teams_match("CF Cruz Azul", "Cruz Azul")
        assert teams_match("Manchester United FC", "Manchester United")

    def test_does_not_confuse_same_city_rivals(self):
        """The mistake that produces a confident signal on the wrong game."""
        assert not teams_match("Manchester United", "Manchester City")

    def test_rejects_empty(self):
        assert not teams_match("", "Arsenal")

    def test_does_not_match_womens_fixture_to_mens(self):
        """A different event entirely - and token-subset logic alone merges them."""
        assert not teams_match("Arsenal Women", "Arsenal")
        assert teams_match("Arsenal Women", "Arsenal WFC")

    def test_resolves_common_abbreviations(self):
        assert teams_match("Man Utd", "Manchester United")

    def test_does_not_match_different_clubs_sharing_a_word(self):
        assert not teams_match("Real Madrid", "Real Sociedad")
        assert not teams_match("Inter Milan", "AC Milan")


class TestDivergenceStrategy:
    def _setup(self, ask, fair_odds=(2.0, 2.0)):
        market = Market(
            venue=Venue.POLYMARKET, market_id="m1", event_id="e1",
            title="Cruz Azul", category="sports", yes_ask=ask,
            close_ts=SOON, fee_rate=0.05,
        )
        event = Event(venue=Venue.POLYMARKET, event_id="e1",
                      title="CF Cruz Azul vs. Club Puebla", category="sports",
                      markets=[market])
        odds = OddsEvent("o1", "soccer_mex", "Club Puebla", "CF Cruz Azul", SOON,
                         [BookLine("pinnacle", {"CF Cruz Azul": fair_odds[0],
                                                "Club Puebla": fair_odds[1]})])
        return market, event, odds

    def test_flags_stale_polymarket_price(self):
        """Sharp says 50%, Polymarket asks 40c -> real edge."""
        market, event, odds = self._setup(0.40)
        sig = SportsbookDivergence().evaluate(market, event, odds, "CF Cruz Azul")
        assert sig is not None
        assert sig.est_probability == pytest.approx(0.5, abs=1e-3)
        assert sig.edge > 0.15
        assert not sig.deterministic
        assert "wrong side" in sig.counter_case

    def test_no_signal_when_prices_agree(self):
        market, event, odds = self._setup(0.50)
        assert SportsbookDivergence().evaluate(
            market, event, odds, "CF Cruz Azul") is None

    def test_fees_are_deducted(self):
        """A gap smaller than the fee drag must not produce a signal."""
        market, event, odds = self._setup(0.495)
        assert SportsbookDivergence(min_edge=0.02).evaluate(
            market, event, odds, "CF Cruz Azul") is None

    def test_implausible_gap_loses_confidence(self):
        """A huge divergence is more likely a mismatch than a gift."""
        _, _, odds = self._setup(0.10)
        market, event, _ = self._setup(0.10)
        sig = SportsbookDivergence().evaluate(market, event, odds, "CF Cruz Azul")
        assert sig is not None and sig.confidence < 0.5

    def test_match_requires_both_teams(self):
        market, event, odds = self._setup(0.40)
        event.title = "Cruz Azul Winner"      # opponent missing
        market.title = "Cruz Azul"
        assert SportsbookDivergence().match(market, event, [odds]) is None

    def test_match_rejects_distant_start_time(self):
        market, event, odds = self._setup(0.40)
        odds.commence_time = SOON + timedelta(hours=48)
        assert SportsbookDivergence().match(market, event, [odds]) is None

    def test_match_skips_ambiguous_draw_market(self):
        market, event, odds = self._setup(0.40)
        market.title = "Draw (CF Cruz Azul vs. Club Puebla)"
        assert SportsbookDivergence().match(market, event, [odds]) is None

    def test_match_succeeds_on_clean_pair(self):
        market, event, odds = self._setup(0.40)
        matched = SportsbookDivergence().match(market, event, [odds])
        assert matched is not None
        assert matched[1] == "CF Cruz Azul"
