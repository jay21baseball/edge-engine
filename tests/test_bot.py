"""American odds, message handling, and the conversational bankroll flow."""
import pytest

from edge_engine.alert.briefing import (
    DAILY,
    WEEKLY,
    BriefingWindow,
    build_briefing,
)
from edge_engine.alert.format import (
    american_str,
    edge_points,
    from_american,
    payout,
    to_american,
)
from edge_engine.bot.commands import HANDLERS
from edge_engine.bot.listener import TelegramBot, parse_number, split_message
from edge_engine.ingest.models import Venue
from edge_engine.scan import Engine, load_config
from edge_engine.strategies.base import Signal


@pytest.fixture
def engine(tmp_path):
    config = load_config("does-not-exist.yaml")
    config["db_path"] = str(tmp_path / "t.db")
    return Engine(config)


class TestAmericanOdds:
    def test_even_money(self):
        assert to_american(0.50) == -100

    def test_underdog_is_positive(self):
        # risk 33 to win 67 -> +203
        assert to_american(0.33) == 203
        assert american_str(0.33) == "+203"

    def test_favourite_is_negative(self):
        # risk 66 to win 34 -> -194
        assert to_american(0.66) == -194
        assert american_str(0.66) == "-194"

    def test_round_trips_back_to_probability(self):
        for price in (0.10, 0.25, 0.45, 0.55, 0.80, 0.95):
            recovered = from_american(to_american(price))
            assert recovered == pytest.approx(price, abs=0.005)

    def test_degenerate_prices(self):
        assert to_american(0.0) is None
        assert to_american(1.0) is None
        assert american_str(1.5) == "n/a"

    def test_payout_matches_the_quoted_odds(self):
        """Risking $100 at +203 should win about $203."""
        assert payout(100, 0.33) == pytest.approx(203, abs=1.0)

    def test_edge_points(self):
        assert edge_points(0.54, 0.45) == pytest.approx(9.0)


class TestMessageSplitting:
    def test_short_message_untouched(self):
        assert split_message("hello") == ["hello"]

    def test_splits_on_blank_lines(self):
        text = "\n\n".join(["block " + "x" * 200 for _ in range(40)])
        chunks = split_message(text, limit=1000)
        assert len(chunks) > 1
        assert all(len(c) <= 1000 for c in chunks)

    def test_never_splits_inside_a_tag(self):
        text = "\n\n".join([f"<b>heading {i}</b> body" for i in range(300)])
        for chunk in split_message(text, limit=500):
            assert chunk.count("<b>") == chunk.count("</b>")


class TestNumberParsing:
    @pytest.mark.parametrize("text,expected", [
        ("2500", 2500), ("$2,500", 2500), ("2.5k", 2500),
        ("  1000  ", 1000), ("$10,000.50", 10000.50),
    ])
    def test_accepts_common_formats(self, text, expected):
        assert parse_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["hello", "/dailyedge", "", "abc123"])
    def test_rejects_non_numbers(self, text):
        assert parse_number(text) is None


class TestCommandParsing:
    def test_slash_command(self):
        assert TelegramBot.parse_command("/dailyedge") == ("dailyedge", [])

    def test_command_with_args(self):
        assert TelegramBot.parse_command("/took 1 46") == ("took", ["1", "46"])

    def test_strips_bot_suffix(self):
        assert TelegramBot.parse_command("/status@ForgeCom_bot")[0] == "status"

    @pytest.mark.parametrize("phrase", [
        "daily edge", "Daily Edge", "today's edge", "any plays",
    ])
    def test_natural_phrasing(self, phrase):
        assert TelegramBot.parse_command(phrase)[0] == "dailyedge"

    def test_weekly_phrasing(self):
        assert TelegramBot.parse_command("weekly edge")[0] == "weeklyedge"

    def test_unknown_text(self):
        assert TelegramBot.parse_command("what is the weather")[0] is None


class TestBankrollFlow:
    def test_briefing_asks_for_bankroll_first(self, engine):
        reply = HANDLERS["dailyedge"](engine, "chat1", [], lambda m: None)
        assert "bankroll" in reply.lower()
        assert engine.store.get_state("chat1", "pending") is not None

    def test_setting_bankroll_resizes_everything(self, engine):
        HANDLERS["bankroll"](engine, "chat1", ["5000"], None)
        assert engine.bankroll.bankroll == 5000
        assert engine.bankroll.unit_size() == pytest.approx(250.0)

    def test_bankroll_accepts_formatted_input(self, engine):
        HANDLERS["bankroll"](engine, "chat1", ["$2,500"], None)
        assert engine.bankroll.bankroll == 2500

    def test_rejects_unusable_bankroll(self, engine):
        reply = HANDLERS["bankroll"](engine, "chat1", ["10"], None)
        assert "nothing sensible" in reply.lower()

    def test_tier_note_warns_below_the_arb_gate(self, engine):
        assert "15,000" in HANDLERS["bankroll"](engine, "c", ["2500"], None)

    def test_tier_note_unlocks_above_it(self, engine):
        assert "unlocked" in HANDLERS["bankroll"](engine, "c", ["20000"], None)


def _signal(score_edge, days, title="Market", advisory=False, det=False):
    return Signal(
        strategy="wallet_attention" if advisory else "combinatorial_arb",
        venue=Venue.POLYMARKET, market_id=title, title=title, side="yes",
        entry_price=0.45, est_probability=0.54, edge=score_edge,
        confidence=1.0, days_to_resolution=days, advisory=advisory,
        deterministic=det, stake=None if advisory else 100.0, contracts=200,
    )


class TestBriefingConviction:
    def _state(self, engine):
        return engine.state

    def test_single_dominant_play_is_presented_alone(self, engine):
        signals = [_signal(0.20, 2, "Runaway"), _signal(0.02, 2, "Also-ran")]
        text = build_briefing(signals, self._state(engine), DAILY)
        assert "THE PLAY" in text
        assert "Runaway" in text
        assert "Also-ran" not in text

    def test_comparable_plays_are_all_listed(self, engine):
        signals = [_signal(0.05, 2, "First"), _signal(0.045, 2, "Second")]
        text = build_briefing(signals, self._state(engine), DAILY)
        assert "THE PLAY" not in text
        assert "First" in text and "Second" in text

    def test_daily_excludes_long_dated_but_names_them(self, engine):
        """Long-dated edge is not hidden - it is named, counted, and deferred."""
        signals = [_signal(0.10, 200, "Election 2028")]
        text = build_briefing(signals, self._state(engine), DAILY)
        assert "NO PLAY TODAY" in text
        assert "1 opportunity" in text          # counted
        assert "past this window" in text       # explained
        assert "/weeklyedge or /all" in text    # where to find it

    def test_weekly_uses_its_own_wording(self, engine):
        """The weekly view must not say TODAY or point back at itself."""
        text = build_briefing([_signal(0.10, 200, "X")],
                              self._state(engine), WEEKLY)
        assert "NO PLAYS THIS WEEK" in text
        assert "TODAY" not in text
        assert "/weeklyedge" not in text

    def test_weekly_window_is_wider_than_daily(self, engine):
        assert WEEKLY.max_days > DAILY.max_days
        signals = [_signal(0.10, 8, "This week")]
        assert "NO PLAY" in build_briefing(signals, self._state(engine), DAILY)
        assert "This week" in build_briefing(signals, self._state(engine), WEEKLY)

    def test_advisory_shows_no_stake(self, engine):
        signals = [_signal(0.05, 2, "Research", advisory=True)]
        text = build_briefing(signals, self._state(engine), DAILY)
        assert "NO STAKE" in text
        assert "research only" in text.lower()

    def test_odds_are_rendered_in_american_format(self, engine):
        text = build_briefing([_signal(0.05, 2)], self._state(engine), DAILY)
        assert "+122" in text        # 0.45 -> +122

    def test_circuit_breaker_replaces_the_whole_briefing(self, engine):
        state = self._state(engine)
        state.week_start_bankroll = 2500
        state.current_bankroll = 2000
        text = build_briefing([_signal(0.20, 1)], state, DAILY)
        assert "STAND DOWN" in text
        assert "THE PLAY" not in text

    def test_empty_is_stated_as_correct_not_as_failure(self, engine):
        text = build_briefing([], self._state(engine), DAILY)
        assert "NO PLAY TODAY" in text
        assert "lowering its standards" in text

    def test_total_at_risk_is_reported(self, engine):
        signals = [_signal(0.05, 2, "A"), _signal(0.048, 2, "B")]
        text = build_briefing(signals, self._state(engine), DAILY)
        assert "Total at risk" in text


class TestSignalRoundTrip:
    """Regression: recent_signals omitted id, market_id, and score, so the bot
    could not rehydrate a Signal and /took had nothing to point at."""

    def test_stored_signal_rehydrates(self, engine):
        from edge_engine.bot.commands import _signal_from_row
        original = _signal(0.05, 3.0, "Round trip")
        engine.store.save_signal(original)
        rows = engine.store.recent_signals(limit=5)
        assert rows, "signal was not stored"
        restored = _signal_from_row(rows[0])
        assert restored.title == "Round trip"
        assert restored.entry_price == pytest.approx(original.entry_price)
        assert restored.days_to_resolution == pytest.approx(3.0)

    def test_briefing_index_maps_back_to_ids(self, engine):
        engine.store.save_signal(_signal(0.20, 2.0, "Best"))
        engine.store.save_signal(_signal(0.02, 2.0, "Worst"))
        rows = engine.store.recent_signals(limit=10)
        engine.remember_briefing("c", rows, DAILY)
        ids = engine.store.get_state("c", "briefing_ids")
        assert ids and len(ids) == 2
        top = engine.store.signal_by_id(ids[0])
        assert top["title"] == "Best"     # ordering matches the briefing


class TestJournalCommands:
    def test_index_without_a_briefing_is_rejected(self, engine):
        assert "dailyedge" in HANDLERS["took"](engine, "c", ["1"], None)

    def test_skip_explains_why_passes_are_logged(self, engine):
        engine.store.set_state("c", "briefing_ids", [1])
        engine.store.save_signal(_signal(0.05, 2, "X"))
        reply = HANDLERS["skip"](engine, "c", ["1"], None)
        assert "passed" in reply.lower()
        assert "not being executed" in reply.lower()

    def test_scorecard_runs_with_no_data(self, engine):
        assert "SCORECARD" in HANDLERS["scorecard"](engine, "c", [], None)

    def test_help_lists_the_core_commands(self, engine):
        text = HANDLERS["help"](engine, "c", [], None)
        for command in ("/dailyedge", "/weeklyedge", "/took", "/scorecard"):
            assert command in text
