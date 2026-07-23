"""Push-alert rules.

The failure mode being defended against is not missing an alert — it is firing
so often that real alerts get swiped away with the noise.
"""
from datetime import datetime, timedelta, timezone

import pytest

from edge_engine.alert.rules import (
    DEFAULT_RULES,
    Alerter,
    AlertPolicy,
    format_alert,
)
from edge_engine.ingest.models import Venue
from edge_engine.scan import Engine, load_config
from edge_engine.strategies.base import Signal

NOON = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)
MIDNIGHT = datetime(2026, 7, 22, 2, 0, tzinfo=timezone.utc)


@pytest.fixture
def engine(tmp_path):
    config = load_config("does-not-exist.yaml")
    config["db_path"] = str(tmp_path / "t.db")
    return Engine(config)


def _arb(edge=0.03, market="A1", days=2.0):
    return Signal(
        strategy="combinatorial_arb", venue=Venue.POLYMARKET, market_id=market,
        title="Locked arb", side="buy_all_yes", entry_price=0.95,
        est_probability=1.0, edge=edge, confidence=1.0,
        days_to_resolution=days, deterministic=True, stake=100.0, contracts=105,
    )


def _book(edge=0.09, market="B1", days=1.0, confidence=0.8):
    return Signal(
        strategy="sportsbook_divergence", venue=Venue.POLYMARKET,
        market_id=market, title="Stale line", side="yes", entry_price=0.40,
        est_probability=0.52, edge=edge, confidence=confidence,
        days_to_resolution=days, stake=80.0, contracts=200,
    )


def _research(market="R1"):
    return Signal(
        strategy="wallet_attention", venue=Venue.POLYMARKET, market_id=market,
        title="Research prompt", side="yes", entry_price=0.45,
        est_probability=0.45, edge=0.30, confidence=0.9,
        days_to_resolution=5.0, advisory=True,
    )


class TestBars:
    def test_strong_arb_fires(self, engine):
        assert len(Alerter(engine.store).select([_arb()], "c", NOON)) == 1

    def test_thin_arb_does_not(self, engine):
        assert Alerter(engine.store).select([_arb(edge=0.002)], "c", NOON) == []

    def test_forecast_needs_a_much_higher_bar_than_an_arb(self):
        """A locked arb is arithmetic; a forecast can be wrong."""
        assert (DEFAULT_RULES["sportsbook_divergence"].min_edge >
                DEFAULT_RULES["combinatorial_arb"].min_edge * 4)
        assert (DEFAULT_RULES["wallet_attention"].min_edge >
                DEFAULT_RULES["sportsbook_divergence"].min_edge)

    def test_low_confidence_forecast_blocked(self, engine):
        thin = _book(edge=0.20, confidence=0.2)
        assert Alerter(engine.store).select([thin], "c", NOON) == []

    def test_advisory_never_pushes(self, engine):
        """Research prompts belong in the briefing, not on your lock screen."""
        assert Alerter(engine.store).select([_research()], "c", NOON) == []

    def test_stale_forecast_beyond_window_blocked(self, engine):
        far = _book(edge=0.20, days=30.0)
        assert Alerter(engine.store).select([far], "c", NOON) == []


class TestRestraint:
    def test_daily_cap_enforced(self, engine):
        alerter = Alerter(engine.store, AlertPolicy(max_per_day=2))
        signals = [_arb(market=f"m{i}") for i in range(6)]
        decisions = alerter.select(signals, "c", NOON)
        assert len(decisions) == 2

    def test_cap_counts_previously_sent(self, engine):
        alerter = Alerter(engine.store, AlertPolicy(max_per_day=3))
        first = alerter.select([_arb(market="a")], "c", NOON)
        alerter.record_sent("c", first, NOON)
        second = alerter.select(
            [_arb(market="b"), _arb(market="c"), _arb(market="d")], "c", NOON)
        assert len(second) == 2       # 3 cap minus 1 already sent

    def test_same_market_does_not_re_alert(self, engine):
        alerter = Alerter(engine.store)
        first = alerter.select([_arb(market="dup")], "c", NOON)
        alerter.record_sent("c", first, NOON)
        assert alerter.select([_arb(market="dup")], "c", NOON) == []

    def test_yesterdays_alerts_do_not_count_against_today(self, engine):
        alerter = Alerter(engine.store, AlertPolicy(max_per_day=1))
        yesterday = NOON - timedelta(days=1)
        alerter.record_sent("c", alerter.select([_arb(market="old")], "c",
                                                yesterday), yesterday)
        assert len(alerter.select([_arb(market="new")], "c", NOON)) == 1

    def test_best_first_when_capped(self, engine):
        alerter = Alerter(engine.store, AlertPolicy(max_per_day=1))
        weak, strong = _arb(edge=0.012, market="w"), _arb(edge=0.30, market="s")
        decision = alerter.select([weak, strong], "c", NOON)[0]
        assert decision.signal.market_id == "s"


class TestQuietHours:
    def test_forecast_waits_until_morning(self, engine):
        assert Alerter(engine.store).select([_book()], "c", MIDNIGHT) == []

    def test_locked_arb_pierces_quiet_hours(self, engine):
        """The spread closes when someone else takes it — that will not keep."""
        assert len(Alerter(engine.store).select([_arb()], "c", MIDNIGHT)) == 1

    def test_game_tomorrow_is_not_urgent_enough_to_wake_you(self, engine):
        """The line will still be there at 7am."""
        assert Alerter(engine.store).select([_book(days=1.0)], "c",
                                            MIDNIGHT) == []

    def test_resolving_within_hours_pierces_quiet_hours(self, engine):
        soon = _book(days=0.2)      # ~5 hours out
        assert len(Alerter(engine.store).select([soon], "c", MIDNIGHT)) == 1

    def test_wrapping_window_detection(self):
        policy = AlertPolicy(quiet_start_hour=23, quiet_end_hour=7)
        assert policy.in_quiet_hours(datetime(2026, 7, 22, 23, 30,
                                              tzinfo=timezone.utc))
        assert policy.in_quiet_hours(datetime(2026, 7, 22, 3, 0,
                                              tzinfo=timezone.utc))
        assert not policy.in_quiet_hours(NOON)

    def test_can_be_disabled(self, engine):
        alerter = Alerter(engine.store, AlertPolicy(respect_quiet_hours=False))
        assert len(alerter.select([_book()], "c", MIDNIGHT)) == 1


class TestFormatting:
    def test_alert_is_short_enough_for_a_lock_screen(self, engine):
        decision = Alerter(engine.store).select([_arb()], "c", NOON)[0]
        text = format_alert(decision)
        assert len(text) < 1200
        assert "arb" in text.lower()

    def test_shows_american_odds(self, engine):
        decision = Alerter(engine.store).select([_book()], "c", NOON)[0]
        assert "+150" in format_alert(decision)      # 0.40 -> +150

    def test_reads_like_a_plain_heads_up(self, engine):
        """No box-drawing bars, no monospace label columns, no emoji."""
        text = format_alert(Alerter(engine.store).select([_arb()], "c", NOON)[0])
        for junk in ("━", "─", "<code>", "\U0001f6a8", "⚡"):
            assert junk not in text
        assert "/dailyedge" in text

    def test_urgent_flag_is_still_computed(self, engine):
        urgent = Alerter(engine.store).select([_arb()], "c", NOON)[0]
        assert urgent.urgent


class TestEngineIntegration:
    def test_no_chat_id_means_no_push(self, engine):
        engine.config["telegram_chat_id"] = None
        assert engine.push_alerts([_arb()]) == 0

    def test_push_records_only_what_was_sent(self, engine):
        engine.config["telegram_chat_id"] = "c"
        engine.alerter.policy.max_per_day = 2
        sent = engine.push_alerts([_arb(market=f"m{i}") for i in range(5)])
        assert sent == 2
        log = engine.store.get_state("c", "alert_log", [])
        assert len(log) == 2, "recorded a different count than it sent"
