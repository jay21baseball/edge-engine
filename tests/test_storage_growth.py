"""Snapshot storage must stay bounded.

Regression: snapshotting all ~88k markets on every scan reached 2.2 GB in a
day of testing — past the entire Supabase free tier, which would have made the
cloud deployment fail within an hour of going live.
"""
from datetime import datetime, timedelta, timezone

import pytest

from edge_engine.ingest.models import Market, Venue
from edge_engine.store.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    return SqliteStore(tmp_path / "t.db")


def _market(mid="m1", yes_ask=0.50, yes_bid=0.49):
    return Market(
        venue=Venue.POLYMARKET, market_id=mid, event_id="e1", title=mid,
        category="politics", yes_bid=yes_bid, yes_ask=yes_ask,
        no_bid=round(1 - yes_ask, 4), no_ask=round(1 - yes_bid, 4),
        close_ts=datetime.now(timezone.utc) + timedelta(days=5),
    )


def _snapshot_count(store) -> int:
    with store.connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM market_snapshots").fetchone()[0]


class TestChangeDetection:
    def test_first_scan_stores_everything(self, store):
        store.upsert_markets([_market(f"m{i}") for i in range(20)])
        assert _snapshot_count(store) == 20

    def test_unchanged_rescan_stores_nothing(self, store):
        markets = [_market(f"m{i}") for i in range(20)]
        store.upsert_markets(markets)
        store.upsert_markets(markets)
        store.upsert_markets(markets)
        assert _snapshot_count(store) == 20, "unchanged quotes were re-stored"

    def test_only_movers_are_stored(self, store):
        markets = [_market(f"m{i}") for i in range(20)]
        store.upsert_markets(markets)
        markets[3].yes_ask = 0.61
        markets[7].yes_bid = 0.40
        store.upsert_markets(markets)
        assert _snapshot_count(store) == 22      # 20 + the 2 that moved

    def test_return_value_still_reports_markets_seen(self, store):
        """The caller's log should say how many markets were processed, not
        how many rows happened to be written."""
        markets = [_market(f"m{i}") for i in range(20)]
        store.upsert_markets(markets)
        assert store.upsert_markets(markets) == 20

    def test_a_market_can_move_back_and_forth(self, store):
        m = _market("m1", yes_ask=0.50)
        store.upsert_markets([m])
        m.yes_ask = 0.55
        store.upsert_markets([m])
        m.yes_ask = 0.50            # back to the original value
        store.upsert_markets([m])
        assert _snapshot_count(store) == 3, "a genuine move was suppressed"

    def test_growth_is_bounded_over_many_quiet_scans(self, store):
        """96 scans/day with a quiet market must not write 96 rows/market."""
        markets = [_market(f"m{i}") for i in range(50)]
        for _ in range(96):
            store.upsert_markets(markets)
        assert _snapshot_count(store) == 50


class TestRetention:
    def test_prune_removes_old_rows(self, store):
        store.upsert_markets([_market("m1")])
        with store.connect() as conn:
            old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
            conn.execute("UPDATE market_snapshots SET ts = ?", (old,))
        assert store.prune_snapshots(keep_days=90) == 1
        assert _snapshot_count(store) == 0

    def test_prune_keeps_recent_rows(self, store):
        store.upsert_markets([_market("m1")])
        assert store.prune_snapshots(keep_days=90) == 0
        assert _snapshot_count(store) == 1
