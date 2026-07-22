"""Order books, arb detection, sizing, discipline, and wallet screening."""
from datetime import datetime, timedelta, timezone

import pytest

from edge_engine.ingest.kalshi import KalshiClient
from edge_engine.ingest.models import (
    Event,
    LeaderboardEntry,
    Market,
    OrderBook,
    PriceLevel,
    Side,
    Venue,
    WalletActivity,
    WalletPosition,
)
from edge_engine.ingest.polymarket import PolymarketClient
from edge_engine.sizing.bankroll import BankrollConfig, DisciplineState, OpenPosition
from edge_engine.sizing.kelly import kelly_fraction, size_position
from edge_engine.strategies.combinatorial import CombinatorialArb
from edge_engine.strategies.wallet_signal import (
    WalletAttentionQueue,
    detect_market_maker,
    score_wallet,
)

NOW = datetime.now(timezone.utc)
SOON = NOW + timedelta(days=10)


# --------------------------------------------------------------- order books

class TestKalshiOrderBook:
    """Kalshi returns BID ladders for both sides and no asks at all."""

    # Verbatim from the live probe of KXNEXTNATOSECGEN-99-KIOH.
    LIVE = {"orderbook_fp": {
        "no_dollars": [["0.3400", "739.00"], ["0.4600", "315.00"],
                       ["0.8500", "430.00"], ["0.8600", "5.00"]],
        "yes_dollars": [["0.0100", "98.00"], ["0.0200", "66.00"],
                        ["0.0800", "400.00"]],
    }}

    def test_yes_ask_is_derived_from_best_no_bid(self):
        """The market quoted yes_bid=0.08 / yes_ask=0.14; best no bid was 0.86."""
        book = KalshiClient.parse_orderbook("T", self.LIVE)
        assert book.yes_bids[0].price == pytest.approx(0.08)
        assert book.best_ask(Side.YES) == pytest.approx(0.14)  # 1 - 0.86

    def test_reading_yes_ladder_as_asks_would_be_catastrophic(self):
        """Guards the exact mistake most published scanners make."""
        book = KalshiClient.parse_orderbook("T", self.LIVE)
        naive_wrong = 0.01  # cheapest entry in yes_dollars, if misread as an ask
        assert book.best_ask(Side.YES) != pytest.approx(naive_wrong)
        assert book.best_ask(Side.YES) > naive_wrong * 10

    def test_no_ask_derived_from_best_yes_bid(self):
        book = KalshiClient.parse_orderbook("T", self.LIVE)
        assert book.best_ask(Side.NO) == pytest.approx(0.92)  # 1 - 0.08

    def test_ask_sizes_come_from_the_opposing_bid(self):
        book = KalshiClient.parse_orderbook("T", self.LIVE)
        assert book.yes_asks[0].size == pytest.approx(5.0)  # size at no-bid 0.86

    def test_empty_book_is_safe(self):
        book = KalshiClient.parse_orderbook("T", {"orderbook_fp": {}})
        assert book.best_ask(Side.YES) is None
        assert book.cost_to_fill(Side.YES, 10) is None


class TestPolymarketOrderBook:
    RAW = {"bids": [{"price": "0.40", "size": "500"},
                    {"price": "0.39", "size": "900"}],
           "asks": [{"price": "0.42", "size": "300"},
                    {"price": "0.44", "size": "800"}]}

    def test_best_ask_is_cheapest(self):
        book = PolymarketClient.parse_orderbook("tok", self.RAW)
        assert book.best_ask(Side.YES) == pytest.approx(0.42)

    def test_walks_multiple_levels(self):
        book = PolymarketClient.parse_orderbook("tok", self.RAW)
        cost, avg = book.cost_to_fill(Side.YES, 500)
        # 300 @ 0.42 + 200 @ 0.44 = 126 + 88 = 214
        assert cost == pytest.approx(214.0)
        assert avg == pytest.approx(0.428)

    def test_returns_none_when_depth_insufficient(self):
        """A partial fill on an arb leg is a naked position, not a smaller arb."""
        book = PolymarketClient.parse_orderbook("tok", self.RAW)
        assert book.cost_to_fill(Side.YES, 5000) is None


# ------------------------------------------------------------ combinatorial

def _mkt(mid, yes_ask, no_ask, close=SOON, fee_rate=None):
    return Market(venue=Venue.KALSHI, market_id=mid, event_id="E", title=mid,
                  category="politics", yes_ask=yes_ask, no_ask=no_ask,
                  close_ts=close, fee_rate=fee_rate)


def _book(price, size=10_000.0, venue=Venue.KALSHI):
    return OrderBook(market_id="m", venue=venue,
                     yes_asks=[PriceLevel(price, size)],
                     no_asks=[PriceLevel(round(1 - price, 4), size)])


class TestCombinatorialArb:
    def test_rejects_non_mece_events(self):
        ev = Event(venue=Venue.KALSHI, event_id="E", title="t", category="politics",
                   mutually_exclusive=False,
                   markets=[_mkt("a", 0.3, 0.7), _mkt("b", 0.3, 0.7)])
        assert CombinatorialArb().screen(ev) is None

    def test_rejects_misaligned_resolution_dates(self):
        """Legs that settle apart are timing risk, not arbitrage."""
        ev = Event(venue=Venue.KALSHI, event_id="E", title="t", category="politics",
                   mutually_exclusive=True, markets=[
                       _mkt("a", 0.30, 0.70, close=SOON),
                       _mkt("b", 0.30, 0.70, close=SOON + timedelta(days=45)),
                   ])
        assert CombinatorialArb().screen(ev) is None

    def test_nato_case_rejected_in_phase_one(self):
        """The live 8-leg Kalshi set: 2% gross, ~6% fee burden -> must be rejected.

        This is the false positive every naive scanner emits.
        """
        prices = [0.14, 0.10, 0.10, 0.10, 0.10, 0.12, 0.17, 0.15]
        ev = Event(venue=Venue.KALSHI, event_id="NATO", title="NATO SecGen",
                   category="politics", mutually_exclusive=True,
                   markets=[_mkt(f"c{i}", p, round(1 - p, 4))
                            for i, p in enumerate(prices)])
        assert sum(prices) == pytest.approx(0.98)   # looks like free money
        assert CombinatorialArb().screen(ev) is None  # correctly refused

    def test_same_book_survives_on_zero_fee_geopolitics(self):
        """Identical prices on a zero-fee venue: the 2% is real."""
        prices = [0.14, 0.10, 0.10, 0.10, 0.10, 0.12, 0.17, 0.15]
        markets = [Market(venue=Venue.POLYMARKET, market_id=f"c{i}", event_id="G",
                          title=f"c{i}", category="geopolitics", yes_ask=p,
                          no_ask=round(1 - p, 4), close_ts=SOON, fee_rate=0.0)
                   for i, p in enumerate(prices)]
        ev = Event(venue=Venue.POLYMARKET, event_id="G", title="Geo",
                   category="geopolitics", neg_risk=True, markets=markets)
        candidate = CombinatorialArb().screen(ev)
        assert candidate is not None
        assert candidate["fee_burden"] == 0.0

        sig = CombinatorialArb(min_net_edge=0.005).verify(
            candidate, lambda m: _book(m.yes_ask, venue=Venue.POLYMARKET), contracts=100
        )
        assert sig is not None
        assert sig.deterministic is True
        assert sig.edge == pytest.approx(0.0204, abs=1e-3)
        assert sig.rationale["net_profit"] == pytest.approx(2.0, abs=0.01)

    def test_rejects_when_depth_cannot_fill_every_leg(self):
        prices = [0.20, 0.20, 0.20, 0.20]
        markets = [Market(venue=Venue.POLYMARKET, market_id=f"c{i}", event_id="G",
                          title=f"c{i}", category="geopolitics", yes_ask=p,
                          no_ask=round(1 - p, 4), close_ts=SOON, fee_rate=0.0)
                   for i, p in enumerate(prices)]
        ev = Event(venue=Venue.POLYMARKET, event_id="G", title="Geo",
                   category="geopolitics", neg_risk=True, markets=markets)
        candidate = CombinatorialArb().screen(ev)
        assert candidate is not None  # 1 - 0.80 = 0.20 gross, zero fees

        thin = lambda m: _book(m.yes_ask, size=5.0, venue=Venue.POLYMARKET)
        assert CombinatorialArb().verify(candidate, thin, contracts=100) is None

    def test_phantom_arb_at_top_of_book_dies_on_the_walk(self):
        """Quoted price implies an arb; real depth exists only for 2 contracts."""
        markets = [Market(venue=Venue.POLYMARKET, market_id=f"c{i}", event_id="G",
                          title=f"c{i}", category="geopolitics", yes_ask=0.20,
                          no_ask=0.80, close_ts=SOON, fee_rate=0.0)
                   for i in range(4)]
        ev = Event(venue=Venue.POLYMARKET, event_id="G", title="Geo",
                   category="geopolitics", neg_risk=True, markets=markets)
        candidate = CombinatorialArb().screen(ev)

        def shallow(_m):
            return OrderBook(market_id="m", venue=Venue.POLYMARKET, yes_asks=[
                PriceLevel(0.20, 2.0), PriceLevel(0.45, 10_000.0),
            ])

        sig = CombinatorialArb().verify(candidate, shallow, contracts=100)
        assert sig is None  # avg fill ~0.445/leg -> no arb


# ------------------------------------------------------------------- sizing

class TestKelly:
    def test_no_edge_means_no_bet(self):
        assert kelly_fraction(0.50, 0.50) == 0.0

    def test_negative_edge_never_returns_negative(self):
        assert kelly_fraction(0.40, 0.60) == 0.0
        s = size_position(0.40, 0.60, 2500)
        assert s.contracts == 0 and s.stake == 0

    def test_bigger_edge_bets_more(self):
        assert kelly_fraction(0.70, 0.50) > kelly_fraction(0.60, 0.50)

    def test_quarter_kelly_is_a_quarter_of_full(self):
        s = size_position(0.70, 0.50, 10_000, kelly_multiplier=0.25,
                          max_single_position_pct=100)
        assert s.kelly_used == pytest.approx(s.kelly_full * 0.25)

    def test_position_cap_binds(self):
        s = size_position(0.95, 0.50, 10_000, kelly_multiplier=1.0,
                          max_single_position_pct=5.0)
        assert s.capped_by == "max_single_position_pct"
        assert s.stake <= 500.01

    def test_respects_min_order_size(self):
        """Polymarket's 5-share minimum can make a tiny edge unactionable."""
        s = size_position(0.51, 0.50, 20, min_order_size=5.0)
        assert not s.is_actionable
        assert s.capped_by == "below_min_order_size"

    def test_available_capital_caps_stake(self):
        s = size_position(0.90, 0.50, 10_000, available_capital=100.0,
                          max_single_position_pct=100)
        assert s.capped_by == "available_capital"
        assert s.stake <= 100.01


class TestDiscipline:
    def _state(self, **kw):
        return DisciplineState(config=BankrollConfig(**kw))

    def test_small_bankroll_raises_the_edge_floor(self):
        """Counterintuitive but correct: smaller stack, HIGHER bar."""
        assert BankrollConfig(bankroll=500).effective_min_edge > \
               BankrollConfig(bankroll=50_000).effective_min_edge

    def test_cross_venue_gated_at_small_bankroll(self):
        st = self._state(bankroll=2500)
        ok, why = st.can_trade("cross_venue_arb")
        assert not ok and "15,000" in why

    def test_cross_venue_unlocks_at_the_gate(self):
        assert self._state(bankroll=20_000).can_trade("cross_venue_arb")[0]

    def test_daily_trade_cap(self):
        st = self._state(bankroll=2500, max_trades_per_day=2)
        for i in range(2):
            st.record_trade(OpenPosition(i, f"m{i}", 50.0, NOW))
        ok, why = st.can_trade("combinatorial_arb")
        assert not ok and "Daily trade cap" in why

    def test_circuit_breaker_halts_everything(self):
        st = self._state(bankroll=2500, drawdown_circuit_breaker_pct=15)
        st.week_start_bankroll = 2500
        st.current_bankroll = 2000  # -20%
        assert st.circuit_breaker_tripped
        ok, why = st.can_trade("combinatorial_arb")
        assert not ok and "CIRCUIT BREAKER" in why

    def test_exposure_cap_blocks_new_positions(self):
        st = self._state(bankroll=2500, max_concurrent_exposure_pct=40)
        st.record_trade(OpenPosition(1, "m", 1000.0, NOW))
        assert st.available_capital == pytest.approx(0.0)
        assert not st.can_trade("combinatorial_arb")[0]

    def test_edge_below_floor_rejected(self):
        st = self._state(bankroll=2500, min_edge_threshold_pct=4.0)
        assert not st.can_trade("combinatorial_arb", edge=0.01)[0]
        assert st.can_trade("combinatorial_arb", edge=0.10)[0]

    def test_locked_arb_gets_a_lower_floor_than_a_forecast(self):
        """The floor buys cushion against variance and estimation error.

        A deterministic arb carries neither, so holding it to the forecast bar
        would reject genuinely free money.
        """
        st = self._state(bankroll=2500, min_edge_threshold_pct=4.0)
        assert st.min_edge_for(deterministic=True) < st.min_edge_for(False)
        # A 2% locked arb is real money; a 2% forecast is inside the error bars.
        assert st.can_trade("combinatorial_arb", edge=0.02, deterministic=True)[0]
        assert not st.can_trade("wallet_attention", edge=0.02)[0]

    def test_locked_arb_floor_still_covers_execution_risk(self):
        st = self._state(bankroll=2500)
        assert st.min_edge_for(deterministic=True) > 0
        assert not st.can_trade("combinatorial_arb", edge=0.0001,
                                deterministic=True)[0]


# ------------------------------------------------------------------ wallets

def _pos(mid, avg, cur, size=100.0, side=Side.YES, pnl=50.0, redeemable=True):
    return WalletPosition(address="0xa", market_id=mid, title=f"m{mid}", side=side,
                          size=size, avg_price=avg, current_price=cur,
                          cash_pnl=pnl, redeemable=redeemable)


class TestMarketMakerDetection:
    def test_swisstony_profile_excluded(self):
        """Live rank-1 wallet: $8.56M P&L on $376M volume = 44:1."""
        entry = LeaderboardEntry("0xa", "swisstony", 1, 8_558_204, 376_263_887,
                                 "MONTH", "OVERALL")
        assert entry.volume_to_pnl == pytest.approx(44.0, abs=0.5)
        is_mm, _, reasons = detect_market_maker(entry, [], [])
        assert is_mm and "volume/pnl" in reasons[0]

    def test_extreme_turnover_wallet_excluded(self):
        """Live rank-10: 155:1."""
        entry = LeaderboardEntry("0xb", "", 10, 2_082_338, 323_354_228,
                                 "MONTH", "OVERALL")
        assert detect_market_maker(entry, [], [])[0]

    def test_plausible_directional_trader_not_excluded(self):
        """Live rank-17 FootballFan98: 2.3:1."""
        entry = LeaderboardEntry("0xc", "FootballFan98", 17, 1_551_053, 3_602_510,
                                 "MONTH", "OVERALL")
        assert entry.volume_to_pnl < 3
        assert not detect_market_maker(entry, [], [])[0]

    def test_two_sided_inventory_excluded(self):
        entry = LeaderboardEntry("0xd", "", 5, 100_000, 200_000, "MONTH", "OVERALL")
        positions = []
        for i in range(4):
            positions.append(_pos(f"m{i}", 0.4, 1.0, side=Side.YES))
            positions.append(_pos(f"m{i}", 0.6, 0.0, side=Side.NO))
        is_mm, two_sided, reasons = detect_market_maker(entry, positions, [])
        assert is_mm and two_sided == 4

    def test_round_tripping_excluded(self):
        entry = LeaderboardEntry("0xe", "", 5, 100_000, 300_000, "MONTH", "OVERALL")
        acts = []
        for i in range(6):
            for side in ("BUY", "SELL", "BUY", "SELL"):
                acts.append(WalletActivity("0xe", NOW, f"m{i}", "t", side,
                                           "TRADE", 10, 5, 0.5))
        assert detect_market_maker(entry, [], acts)[0]


class TestWalletScoring:
    ENTRY = LeaderboardEntry("0xa", "sharp", 40, 500_000, 1_500_000,
                             "MONTH", "SPORTS")

    def _acts(self, n=40):
        return [WalletActivity("0xa", NOW - timedelta(days=3), f"m{i}", "t",
                               "BUY", "TRADE", 10.0, 5.0, 0.5) for i in range(n)]

    def test_favorite_buyer_with_no_real_edge_is_rejected(self):
        """Buys 90c favorites, wins 90% -> exactly fair value -> ZERO skill.

        The public leaderboard would show this wallet as a 90% win rate.
        """
        positions = [_pos(f"m{i}", 0.90, 1.0) for i in range(36)]
        positions += [_pos(f"m{i}", 0.90, 0.0, pnl=0.0) for i in range(36, 40)]
        s = score_wallet("0xa", self.ENTRY, positions, self._acts())
        assert s.n_resolved == 40
        assert s.entry_adjusted_edge == pytest.approx(0.0, abs=0.02)
        assert not s.qualified

    def test_genuine_edge_qualifies(self):
        """A realistic +5% edge: 55% win rate on 50c contracts.

        Needs ~240 samples to clear t=1.5 - binary outcomes at 50c carry enough
        variance that a real edge takes hundreds of trades to prove. That is the
        statistics being honest, not the gate being harsh.
        """
        positions = [_pos(f"m{i}", 0.50, 1.0) for i in range(165)]
        positions += [_pos(f"m{i}", 0.50, 0.0, pnl=0.0) for i in range(165, 300)]
        s = score_wallet("0xa", self.ENTRY, positions, self._acts(300))
        assert s.entry_adjusted_edge == pytest.approx(0.05, abs=0.01)
        assert s.qualified, s.disqualified_for

    def test_small_sample_rejected(self):
        positions = [_pos(f"m{i}", 0.20, 1.0) for i in range(5)]
        s = score_wallet("0xa", self.ENTRY, positions, self._acts(5))
        assert not s.qualified
        assert any("resolved positions" in r for r in s.disqualified_for)

    def test_concentrated_pnl_rejected(self):
        """The rank-3 pattern: one lottery ticket carrying everything."""
        positions = [_pos(f"m{i}", 0.50, 1.0, pnl=1.0) for i in range(39)]
        positions.append(_pos("big", 0.50, 1.0, pnl=100_000.0))
        s = score_wallet("0xa", self.ENTRY, positions, self._acts())
        assert s.pnl_herfindahl > 0.9
        assert not s.qualified
        assert any("concentration" in r for r in s.disqualified_for)

    def test_noisy_edge_fails_t_stat(self):
        """Positive mean edge driven by variance, not consistency."""
        positions = [_pos(f"m{i}", 0.50, 1.0, pnl=10.0) for i in range(21)]
        positions += [_pos(f"m{i}", 0.50, 0.0, pnl=0.0) for i in range(21, 40)]
        s = score_wallet("0xa", self.ENTRY, positions, self._acts())
        assert s.entry_adjusted_edge < 0.06
        assert not s.qualified


class TestAttentionQueue:
    def _qualified(self, addr):
        """A wallet with a realistic, statistically significant +5% edge."""
        positions = [_pos(f"x{i}", 0.50, 1.0) for i in range(165)]
        positions += [_pos(f"x{i}", 0.50, 0.0, pnl=0.0) for i in range(165, 300)]
        entry = LeaderboardEntry(addr, addr, 5, 500_000, 1_000_000, "MONTH", "SPORTS")
        acts = [WalletActivity(addr, NOW - timedelta(days=1), "m", "t", "BUY",
                               "TRADE", 10.0, 5.0, 0.5) for _ in range(300)]
        score = score_wallet(addr, entry, positions, acts)
        assert score.qualified, score.disqualified_for
        return score

    def test_requires_multiple_agreeing_wallets(self):
        scores = {"0xa": self._qualified("0xa")}
        live = WalletPosition("0xa", "M1", "Market 1", Side.YES, 100, 0.40,
                              current_price=0.45, redeemable=False)
        sigs = WalletAttentionQueue().build(
            scores, {"0xa": [live]}, {"M1": 0.45})
        assert sigs == []

    def test_two_agreeing_wallets_surface(self):
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.YES, 100, 0.40,
                               current_price=0.45, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        sigs = WalletAttentionQueue().build(scores, positions, {"M1": 0.45})
        assert len(sigs) == 1
        assert sigs[0].rationale["agreeing_wallets"] == 2
        assert not sigs[0].deterministic

    def test_suppressed_when_move_already_happened(self):
        """They entered at 0.40, it is now 0.75 - 58% of the move is gone."""
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.YES, 100, 0.40,
                               current_price=0.75, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        assert WalletAttentionQueue(max_edge_decay=0.5).build(
            scores, positions, {"M1": 0.75}) == []

    def test_no_holders_priced_against_the_no_side(self):
        """Regression: NO entries were compared against the YES quote.

        That produced edges of +44% and 'move already gone' of -683%.
        """
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.NO, 100, 0.88,
                               current_price=0.91, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        # YES quote of 0.09 means the NO price is 0.91 - the side they hold.
        sig = WalletAttentionQueue().build(scores, positions, {"M1": 0.09})[0]
        assert sig.entry_price == pytest.approx(0.91)
        assert 0 <= sig.rationale["move_already_captured_pct"] <= 100
        assert sig.edge < 0.2

    def test_capture_never_goes_negative(self):
        """A price moving AGAINST them is 0% captured, not negative decay."""
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.YES, 100, 0.60,
                               current_price=0.40, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        sig = WalletAttentionQueue().build(scores, positions, {"M1": 0.40})[0]
        assert sig.rationale["move_already_captured_pct"] == 0.0

    def test_uses_real_resolution_horizon(self):
        """A 2028 market must not be ranked as if it resolves in a week."""
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.YES, 100, 0.40,
                               current_price=0.45, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        sig = WalletAttentionQueue().build(
            scores, positions, {"M1": 0.45}, days_to_resolution={"M1": 900.0})[0]
        assert sig.days_to_resolution == pytest.approx(900.0)
        fast = WalletAttentionQueue().build(
            scores, positions, {"M1": 0.45}, days_to_resolution={"M1": 2.0})[0]
        assert fast.score > sig.score * 100

    def test_alert_states_decayed_edge_and_hidden_leg_risk(self):
        scores = {a: self._qualified(a) for a in ("0xa", "0xb")}
        positions = {
            a: [WalletPosition(a, "M1", "Market 1", Side.YES, 100, 0.40,
                               current_price=0.50, redeemable=False)]
            for a in ("0xa", "0xb")
        }
        sig = WalletAttentionQueue().build(scores, positions, {"M1": 0.50})[0]
        assert sig.rationale["move_already_captured_pct"] == pytest.approx(16.7, abs=1)
        assert "already gone" in sig.counter_case
        assert "hedged" in sig.counter_case


class TestSignalRanking:
    def test_ranks_on_edge_per_day_not_raw_edge(self):
        """A 3% edge in 2 days must outrank an 8% edge in 240 days."""
        from edge_engine.strategies.base import Signal
        fast = Signal("s", Venue.KALSHI, "a", "fast", "yes", 0.5, 0.6,
                      edge=0.03, confidence=1.0, days_to_resolution=2)
        slow = Signal("s", Venue.KALSHI, "b", "slow", "yes", 0.5, 0.6,
                      edge=0.08, confidence=1.0, days_to_resolution=240)
        assert fast.score > slow.score
        assert fast.score / slow.score > 40

    def test_deterministic_signals_get_a_premium(self):
        from edge_engine.strategies.base import Signal
        arb = Signal("s", Venue.KALSHI, "a", "arb", "yes", 0.5, 1.0,
                     edge=0.03, confidence=1.0, days_to_resolution=5,
                     deterministic=True)
        est = Signal("s", Venue.KALSHI, "b", "est", "yes", 0.5, 0.6,
                     edge=0.03, confidence=1.0, days_to_resolution=5)
        assert arb.score > est.score
