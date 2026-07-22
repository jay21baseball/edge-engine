"""Why did 3,778 MECE events die on leg count, and how close were the 56 near-misses?"""
import logging
from collections import Counter

from edge_engine.ingest.models import Venue
from edge_engine.scan import Engine, load_config
from edge_engine.sizing.fees import min_viable_gross_edge

logging.basicConfig(level=logging.WARNING)
engine = Engine(load_config())
events = engine.fetch_events()

legs = Counter()
big = []
for ev in events:
    if not ev.is_mece:
        continue
    n = len([m for m in ev.markets if m.status == "open"])
    if n < 2:
        legs["<2 (only one leg still open)"] += 1
    elif n <= 20:
        legs["2-20 (scanned)"] += 1
    elif n <= 50:
        legs["21-50 (EXCLUDED)"] += 1
        big.append(ev)
    else:
        legs[">50 (EXCLUDED)"] += 1
        big.append(ev)

print("===== LEG-COUNT DISTRIBUTION OF MECE EVENTS =====")
for k, v in sorted(legs.items(), key=lambda kv: -kv[1]):
    print(f"{k:>32}: {v:>5}")

print("\n===== EXCLUDED LARGE-N EVENTS BY VENUE/CATEGORY =====")
bycat = Counter((e.venue.value, e.category) for e in big)
for (venue, cat), count in bycat.most_common(12):
    print(f"  {venue:<11} {cat[:24]:<26} {count}")

print("\n===== NEAR-MISSES: how far were the 56 from clearing? =====")
misses = []
for ev in events:
    if not ev.is_mece:
        continue
    markets = [m for m in ev.markets if m.status == "open"]
    if not (2 <= len(markets) <= 60):
        continue
    stamps = [m.close_ts for m in markets if m.close_ts]
    if len(stamps) < len(markets):
        continue
    if (max(stamps) - min(stamps)).total_seconds() / 86400.0 > 1.0:
        continue
    yes = [m.yes_ask for m in markets]
    no = [m.no_ask for m in markets]
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
        continue
    override = markets[0].fee_rate if ev.venue is Venue.POLYMARKET else None
    burden = min_viable_gross_edge(best[2], ev.venue, category=ev.category,
                                   rate_override=override)
    misses.append((best[1] - burden, best[1], burden, len(markets), ev))

misses.sort(key=lambda x: -x[0])
print(f"{'net':>9} {'gross':>9} {'burden':>9} {'legs':>5}  venue/category")
for net, gross, burden, n, ev in misses[:20]:
    print(f"{net:>+9.4f} {gross:>+9.4f} {burden:>9.4f} {n:>5}  "
          f"{ev.venue.value}/{ev.category[:14]} {ev.title[:38]}")

zero_fee = [m for m in misses if m[2] == 0.0]
print(f"\nzero-fee-burden candidates (geopolitics): {len(zero_fee)}")
for net, gross, burden, n, ev in zero_fee[:10]:
    print(f"  net={net:+.4f} legs={n} {ev.title[:50]}")
