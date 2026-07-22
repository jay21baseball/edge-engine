"""Diagnostic: where do candidates die? Distinguishes "no edge today" from "broken"."""
import logging
from collections import Counter

from edge_engine.ingest.models import Side, Venue
from edge_engine.scan import Engine, load_config
from edge_engine.sizing.fees import min_viable_gross_edge

logging.basicConfig(level=logging.INFO, format="%(message)s")

engine = Engine(load_config())
events = engine.fetch_events()

stats = Counter()
survivors = []

for ev in events:
    stats["total"] += 1
    if not ev.is_mece:
        stats["not_mece"] += 1
        continue
    stats["mece"] += 1

    markets = [m for m in ev.markets if m.status == "open"]
    if not (2 <= len(markets) <= 20):
        stats["bad_leg_count"] += 1
        continue

    stamps = [m.close_ts for m in markets if m.close_ts]
    if len(stamps) < len(markets):
        stats["missing_close_ts"] += 1
        continue
    if (max(stamps) - min(stamps)).total_seconds() / 86400.0 > 1.0:
        stats["resolution_skew"] += 1
        continue
    stats["aligned"] += 1

    yes = [m.yes_ask for m in markets]
    no = [m.no_ask for m in markets]
    if not all(a is not None and 0 < a < 1 for a in yes):
        stats["incomplete_yes_prices"] += 1
        if not all(a is not None and 0 < a < 1 for a in no):
            continue
    stats["priced"] += 1

    n = len(markets)
    best = None
    if all(a is not None and 0 < a < 1 for a in yes):
        g = 1.0 - sum(yes)
        if g > 0:
            best = ("YES", g, list(yes))
    if all(a is not None and 0 < a < 1 for a in no):
        g = (n - 1) - sum(no)
        if g > 0 and (best is None or g > best[1]):
            best = ("NO", g, list(no))

    if best is None:
        stats["no_gross_edge"] += 1
        continue
    stats["gross_positive"] += 1

    rate_override = markets[0].fee_rate if ev.venue is Venue.POLYMARKET else None
    burden = min_viable_gross_edge(best[2], ev.venue, category=ev.category,
                                   rate_override=rate_override)
    if best[1] <= burden:
        stats["killed_by_fees"] += 1
        continue
    stats["passed_phase_1"] += 1
    survivors.append((ev, best, burden))

print("\n===== FUNNEL =====")
for k in ("total", "not_mece", "mece", "bad_leg_count", "missing_close_ts",
          "resolution_skew", "aligned", "incomplete_yes_prices", "priced",
          "no_gross_edge", "gross_positive", "killed_by_fees", "passed_phase_1"):
    print(f"{k:>24}: {stats[k]:>6}")

print("\n===== PHASE-1 SURVIVORS (top 15 by gross) =====")
for ev, best, burden in sorted(survivors, key=lambda x: -x[1][1])[:15]:
    print(f"{ev.venue.value:<11} {ev.category[:12]:<13} legs={len(best[2]):<3} "
          f"{best[0]:<4} gross={best[1]:+.4f} burden={burden:.4f} "
          f"net~{best[1] - burden:+.4f}  {ev.title[:44]}")

if survivors:
    print(f"\n===== PHASE 2: verifying top {min(8, len(survivors))} on real books =====")
    for ev, best, burden in sorted(survivors, key=lambda x: -x[1][1])[:8]:
        cand = engine.arb.screen(ev)
        if cand is None:
            print(f"  {ev.title[:44]:<46} -> screen() disagreed")
            continue
        sig = engine.arb.verify(cand, engine._fetch_book, contracts=100)
        if sig is None:
            print(f"  {ev.title[:44]:<46} -> DIED on book walk (depth/price)")
        else:
            print(f"  {ev.title[:44]:<46} -> SURVIVED net_edge={sig.edge:+.4f} "
                  f"profit=${sig.rationale['net_profit']}")
