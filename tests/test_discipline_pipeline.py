"""Signals must never disappear without an explanation.

Regression: wallet attention signals set est_probability == current price
(they claim no forecast), so Kelly correctly sized them at zero and a branch
with no logging dropped all of them. The scan reported "12 signals" and the
operator received nothing - indistinguishable from a broken scanner.
"""
import pytest

from edge_engine.ingest.models import Venue
from edge_engine.scan import Engine, load_config
from edge_engine.strategies.base import Signal


@pytest.fixture
def engine(tmp_path):
    config = load_config("does-not-exist.yaml")
    config["db_path"] = str(tmp_path / "t.db")
    return Engine(config)


def _advisory(market_id="M1"):
    return Signal(
        strategy="wallet_attention", venue=Venue.POLYMARKET, market_id=market_id,
        title="Attention market", side="yes", entry_price=0.45,
        est_probability=0.45,          # no forecast of its own
        edge=0.03, confidence=0.5, days_to_resolution=7.0, advisory=True,
    )


def _arb(market_id="A1"):
    return Signal(
        strategy="combinatorial_arb", venue=Venue.POLYMARKET, market_id=market_id,
        title="Locked arb", side="buy_all_yes", entry_price=0.98,
        est_probability=1.0, edge=0.02, confidence=1.0,
        days_to_resolution=3.0, deterministic=True,
    )


class TestAdvisorySignals:
    def test_advisory_survives_the_pipeline(self, engine):
        kept = engine._apply_discipline([_advisory()])
        assert len(kept) == 1, "advisory signal was dropped"

    def test_advisory_is_delivered_unsized(self, engine):
        kept = engine._apply_discipline([_advisory()])
        assert kept[0].stake is None
        assert kept[0].contracts is None

    def test_advisory_ignores_the_daily_trade_cap(self, engine):
        """Research prompts are not trades and must not consume the budget."""
        engine.state.trades_today = engine.bankroll.max_trades_per_day
        kept = engine._apply_discipline([_advisory()])
        assert len(kept) == 1

    def test_advisory_below_edge_floor_still_surfaces(self, engine):
        thin = _advisory()
        thin.edge = 0.001          # far below the 4% floor
        assert len(engine._apply_discipline([thin])) == 1

    def test_duplicates_suppressed_within_window(self, engine):
        assert len(engine._apply_discipline([_advisory()])) == 1
        assert engine._apply_discipline([_advisory()]) == []


class TestTradeSignals:
    def test_deterministic_arb_gets_a_stake(self, engine):
        kept = engine._apply_discipline([_arb()])
        assert len(kept) == 1
        assert kept[0].stake and kept[0].stake > 0

    def test_arb_respects_the_daily_trade_cap(self, engine):
        engine.state.trades_today = engine.bankroll.max_trades_per_day
        assert engine._apply_discipline([_arb()]) == []

    def test_circuit_breaker_halts_trades_but_not_research(self, engine):
        engine.state.week_start_bankroll = 2500
        engine.state.current_bankroll = 2000     # -20%, breaker tripped
        assert engine.state.circuit_breaker_tripped
        assert engine._apply_discipline([_arb()]) == []
        assert len(engine._apply_discipline([_advisory()])) == 1
