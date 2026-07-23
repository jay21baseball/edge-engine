"""Whale tracker: dedup, priming, and the big-money message."""
import pytest

from edge_engine.scan import Engine, load_config
from edge_engine.whales.tracker import (
    Whale,
    WhaleTracker,
    format_trade,
    load_whales,
)


@pytest.fixture
def engine(tmp_path):
    config = load_config("does-not-exist.yaml")
    config["db_path"] = str(tmp_path / "t.db")
    return Engine(config)


def _tony():
    return Whale(name="Tony", address="0xabc", username="swisstony",
                 big_usdc=10000.0)


def _trade(hash_, usdc, side="BUY", outcome="Yankees", price=0.62,
           size=100, ts=1_784_600_000, title="Yankees vs Red Sox"):
    return {"hash": hash_, "ts": ts, "side": side, "usdc": usdc,
            "price": price, "size": size, "outcome": outcome,
            "title": title, "slug": "yankees-red-sox"}


class FakePoly:
    def __init__(self, trades):
        self.trades = trades
    def recent_trades(self, address, limit=40):
        return self.trades
    def portfolio_value(self, address):
        return 472000.0


class TestBigMoneyMessage:
    def test_big_trade_gets_the_loud_message(self):
        text = format_trade(_tony(), _trade("h1", 15000))
        assert "big one" in text.lower()
        assert "$15,000" in text
        assert "bets this heavy" in text

    def test_normal_trade_gets_the_normal_message(self):
        text = format_trade(_tony(), _trade("h1", 1500))
        assert "big one" not in text.lower()
        assert "$1,500" in text

    def test_threshold_is_inclusive(self):
        assert "big one" in format_trade(_tony(), _trade("h1", 10000)).lower()

    def test_message_is_plain_no_special_chars(self):
        text = format_trade(_tony(), _trade("h1", 12000))
        for junk in ("<b>", "<code>", "━", "─", "—", "\U0001f6a8"):
            assert junk not in text
        assert "polymarket.com/@swisstony" in text

    def test_sell_reads_correctly(self):
        text = format_trade(_tony(), _trade("h1", 20000, side="SELL"))
        assert "sold" in text


class TestDedupAndPriming:
    def test_first_run_primes_without_alerting(self, engine):
        tracker = WhaleTracker(FakePoly([_trade("h1", 5000),
                                         _trade("h2", 6000)]), engine.store)
        alerts = tracker.check(_tony())
        assert alerts == [], "first run must prime silently, not replay history"

    def test_new_trade_after_priming_alerts(self, engine):
        poly = FakePoly([_trade("h1", 5000)])
        tracker = WhaleTracker(poly, engine.store)
        tracker.check(_tony())                       # prime
        poly.trades = [_trade("h2", 7000), _trade("h1", 5000)]
        alerts = tracker.check(_tony())
        assert len(alerts) == 1                      # only the new one

    def test_same_trade_not_alerted_twice(self, engine):
        poly = FakePoly([_trade("h1", 5000)])
        tracker = WhaleTracker(poly, engine.store)
        tracker.check(_tony())
        poly.trades = [_trade("h2", 7000), _trade("h1", 5000)]
        tracker.check(_tony())
        assert tracker.check(_tony()) == []          # nothing new the third time

    def test_min_usdc_filters_small_trades(self, engine):
        whale = Whale(name="T", address="0xabc", min_usdc=1000)
        poly = FakePoly([_trade("h1", 500)])
        tracker = WhaleTracker(poly, engine.store)
        tracker.check(whale)                          # prime
        poly.trades = [_trade("h2", 200), _trade("h1", 500)]
        assert tracker.check(whale) == []             # 200 is below the floor


class TestConfig:
    def test_loads_whale_with_defaults(self):
        whales = load_whales({"whales": [{"name": "Tony", "address": "0xabc"}],
                              "whale_big_usdc": 25000})
        assert whales[0].name == "Tony"
        assert whales[0].big_usdc == 25000    # global default applies

    def test_per_whale_big_overrides_global(self):
        whales = load_whales({"whale_big_usdc": 25000, "whales": [
            {"name": "T", "address": "0xabc", "big_usdc": 5000}]})
        assert whales[0].big_usdc == 5000

    def test_skips_whale_without_address(self):
        assert load_whales({"whales": [{"name": "nobody"}]}) == []


class TestSingleShotPoll:
    def test_poll_once_primes_then_reports(self, engine, monkeypatch):
        """The cloud path (whalepoll) must prime on the first run, not spam."""
        engine.config["whales"] = [{"name": "Tony", "address": "0xabc"}]
        sent = []
        monkeypatch.setattr(engine, "_send_to",
                            lambda t, c, text: sent.append(text))
        monkeypatch.setattr(engine.poly, "recent_trades",
                            lambda a, limit=40: [_trade("h1", 12000)])
        monkeypatch.setattr(engine.poly, "portfolio_value", lambda a: 472000.0)
        assert engine.poll_whales_once() == 0        # first run primes
        monkeypatch.setattr(engine.poly, "recent_trades",
                            lambda a, limit=40: [_trade("h2", 30000),
                                                 _trade("h1", 12000)])
        assert engine.poll_whales_once() == 1        # new big trade alerts
        assert "big one" in sent[0].lower()
