"""The odds throttle must survive ephemeral cloud runs.

Under GitHub Actions every scan is a fresh process, so an in-memory timer resets
each pass and would fetch odds on every run: 5,760 requests/month against a 500
free-tier allowance, killing the key in under three days.
"""
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from edge_engine.scan import Engine, load_config


@pytest.fixture
def engine(tmp_path):
    config = load_config("does-not-exist.yaml")
    config["db_path"] = str(tmp_path / "t.db")
    config["scan_interval_seconds"] = 900   # 15 min, matching the workflow
    return Engine(config)


def _at(engine, hour, minute, every=180.0):
    stamp = datetime(2026, 7, 21, hour, minute, tzinfo=timezone.utc)
    with patch("edge_engine.scan.datetime") as mock:
        mock.now.return_value = stamp
        return engine._odds_due(every)


class TestOddsDue:
    @pytest.mark.parametrize("hour", [0, 3, 6, 9, 12, 15, 18, 21])
    def test_fires_on_the_three_hour_boundary(self, engine, hour):
        assert _at(engine, hour, 0)

    @pytest.mark.parametrize("hour,minute", [(1, 0), (2, 30), (4, 45), (7, 15)])
    def test_silent_between_boundaries(self, engine, hour, minute):
        assert not _at(engine, hour, minute)

    def test_fires_once_inside_the_scan_window(self, engine):
        """One 15-min slot per boundary - not the whole hour."""
        assert _at(engine, 6, 10)       # inside the 15-min window
        assert not _at(engine, 6, 20)   # window has passed

    def test_monthly_request_count_fits_free_tier(self, engine):
        """Two sports at a 3-hourly refresh must stay under 500/month."""
        fires = sum(
            1 for h in range(24) for m in range(0, 60, 15) if _at(engine, h, m)
        )
        assert fires == 8                       # 24h / 3h
        assert fires * 2 * 30 == 480            # 2 sports, 30 days
        assert fires * 2 * 30 < 500

    def test_zero_disables_throttle(self, engine):
        assert _at(engine, 7, 23, every=0.0)

    def test_hourly_refresh_would_exceed_free_tier(self, engine):
        """Documents why the default is 180, not 60."""
        fires = sum(
            1 for h in range(24) for m in range(0, 60, 15)
            if _at(engine, h, m, every=60.0)
        )
        assert fires == 24
        assert fires * 2 * 30 > 500     # 1,440/month - needs the paid tier
