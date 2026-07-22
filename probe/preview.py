"""Render a briefing to the console so the formatting can be checked."""
import logging
import re

from edge_engine.alert.briefing import DAILY, WEEKLY, build_briefing
from edge_engine.bot.commands import _signal_from_row
from edge_engine.journal.calibration import build_report
from edge_engine.scan import Engine, load_config

logging.basicConfig(level=logging.WARNING)
engine = Engine(load_config())
engine.set_bankroll("preview", 77.0)

rows = engine.store.recent_signals(limit=40)
signals = [_signal_from_row(r) for r in rows]
report = build_report(engine.store.resolved_predictions())
verdict, detail = report.verdict(), report.detail()

from edge_engine.alert.briefing import BriefingWindow

ALL = BriefingWindow("ALL OPEN EDGE", 3650.0, 12, "NOTHING OPEN", "/dailyedge")

for window in (DAILY, WEEKLY, ALL):
    text = build_briefing(signals, engine.state, window, verdict,
                          calibration_detail=detail)
    print("=" * 70)
    # Strip HTML so the terminal shows what Telegram will lay out.
    print(re.sub(r"<[^>]+>", "", text))
    print()
