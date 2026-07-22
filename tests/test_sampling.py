"""Regression guards for the sampling bugs that silently faked good results.

Both bugs below produced output that looked BETTER than the truth, which is the
dangerous direction. Neither would have been caught by a passing scan.
"""
import pytest

from edge_engine.ingest.polymarket import PolymarketClient


class TestClosedPositionsSampling:
    """`/closed-positions` defaults to realizedPnl DESC, capped at 50 rows.

    Sampling a trader's fifty best trades to judge their skill scored every
    wallet at an impossible +0.5 to +0.75 entry-adjusted edge.
    """

    def test_requests_chronological_order_and_paginates(self, monkeypatch):
        calls = []

        def fake_request(url, params=None, **kw):
            calls.append(params or {})
            if (params or {}).get("offset", 0) >= 100:
                return []
            return [{
                "conditionId": f"c{i}", "title": "t", "outcomeIndex": 0,
                "avgPrice": 0.5, "curPrice": 1.0, "realizedPnl": 1.0, "size": 10,
            } for i in range(50)]

        monkeypatch.setattr("edge_engine.ingest.polymarket.request_json",
                            fake_request)
        client = PolymarketClient()
        result = client.closed_positions("0xabc", limit=150)

        assert calls, "no request was made"
        for params in calls:
            assert params.get("sortBy") == "TIMESTAMP", (
                "sortBy=TIMESTAMP is mandatory - without it the endpoint returns "
                "each wallet's best-50 trades and the skill screen is inverted"
            )
        assert [c.get("offset") for c in calls] == [0, 50, 100]
        assert len(result) == 100

    def test_stops_early_on_short_page(self, monkeypatch):
        def fake_request(url, params=None, **kw):
            return [{
                "conditionId": "c", "title": "t", "outcomeIndex": 0,
                "avgPrice": 0.4, "curPrice": 0.0, "realizedPnl": -1.0, "size": 5,
            }] * 12

        monkeypatch.setattr("edge_engine.ingest.polymarket.request_json",
                            fake_request)
        assert len(PolymarketClient().closed_positions("0xabc", limit=300)) == 12

    def test_rejects_degenerate_entry_prices(self, monkeypatch):
        """avgPrice of 0 or 1 is unusable for entry-adjusted scoring."""
        def fake_request(url, params=None, **kw):
            if (params or {}).get("offset", 0) > 0:
                return []
            return [
                {"conditionId": "a", "title": "t", "outcomeIndex": 0,
                 "avgPrice": 0.0, "curPrice": 1.0, "realizedPnl": 1.0},
                {"conditionId": "b", "title": "t", "outcomeIndex": 0,
                 "avgPrice": 1.0, "curPrice": 1.0, "realizedPnl": 0.0},
                {"conditionId": "c", "title": "t", "outcomeIndex": 0,
                 "avgPrice": 0.45, "curPrice": 1.0, "realizedPnl": 5.0},
            ]

        monkeypatch.setattr("edge_engine.ingest.polymarket.request_json",
                            fake_request)
        result = PolymarketClient().closed_positions("0xabc")
        assert len(result) == 1
        assert result[0].avg_price == pytest.approx(0.45)


class TestImplausibleEdgeGuard:
    """An impossible edge is a bug report, not a signal."""

    def test_implausible_edge_disqualifies(self):
        from datetime import datetime, timedelta, timezone

        from edge_engine.ingest.models import (
            LeaderboardEntry, Side, WalletActivity, WalletPosition,
        )
        from edge_engine.strategies.wallet_signal import score_wallet

        now = datetime.now(timezone.utc)
        # Buys at 0.35, wins every time -> +0.65 edge. Not a trader.
        positions = [
            WalletPosition("0xa", f"m{i}", "t", Side.YES, 10, 0.35,
                           current_price=1.0, cash_pnl=6.5, redeemable=True)
            for i in range(60)
        ]
        acts = [WalletActivity("0xa", now - timedelta(days=2), f"m{i}", "t",
                               "BUY", "TRADE", 10.0, 3.5, 0.35) for i in range(60)]
        entry = LeaderboardEntry("0xa", "toogood", 1, 500_000, 1_000_000,
                                 "MONTH", "SPORTS")
        score = score_wallet("0xa", entry, positions, acts)

        assert score.entry_adjusted_edge > 0.6
        assert not score.qualified
        assert any("IMPLAUSIBLE" in r for r in score.disqualified_for)

    def test_realistic_edge_still_qualifies(self):
        """The +0.035 measured on a correctly-sampled live wallet must pass."""
        from datetime import datetime, timedelta, timezone

        from edge_engine.ingest.models import (
            LeaderboardEntry, Side, WalletActivity, WalletPosition,
        )
        from edge_engine.strategies.wallet_signal import score_wallet

        now = datetime.now(timezone.utc)
        # 55% win rate on 50c contracts = +5% edge. Needs ~240 samples to reach
        # significance, which is the statistics being honest about how much
        # evidence a small real edge actually requires.
        positions = [
            WalletPosition("0xb", f"m{i}", "t", Side.YES, 10,
                           0.50, current_price=1.0, cash_pnl=5.0, redeemable=True)
            for i in range(165)
        ] + [
            WalletPosition("0xb", f"n{i}", "t", Side.YES, 10,
                           0.50, current_price=0.0, cash_pnl=0.0, redeemable=True)
            for i in range(135)
        ]
        acts = [WalletActivity("0xb", now - timedelta(days=2), f"m{i}", "t",
                               "BUY", "TRADE", 10.0, 5.0, 0.5) for i in range(300)]
        entry = LeaderboardEntry("0xb", "sharp", 9, 400_000, 1_000_000,
                                 "MONTH", "SPORTS")
        score = score_wallet("0xb", entry, positions, acts)

        assert 0.02 < score.entry_adjusted_edge < 0.15
        assert score.qualified, score.disqualified_for


class TestLeaderboardRowFiltering:
    """Category leaderboards return rows with vol=0.

    Those sorted to the FRONT of an ascending volume:P&L ranking and consumed
    the entire enrichment budget on wallets with no usable data.
    """

    def test_zero_volume_rows_rank_worst_not_best(self):
        from edge_engine.ingest.models import LeaderboardEntry
        empty = LeaderboardEntry("0xa", "", 1, 1000.0, 0.0, "MONTH", "SPORTS")
        real = LeaderboardEntry("0xb", "", 2, 1000.0, 3000.0, "MONTH", "SPORTS")
        assert empty.volume_to_pnl == 0.0   # sorts first, but is junk
        assert real.volume_to_pnl == 3.0
        usable = [e for e in (empty, real) if e.volume > 0 and e.pnl > 0]
        assert usable == [real]
