"""Is the book walk failing because books are thin, or because fetching is broken?"""
import logging

from edge_engine.ingest.models import Side, Venue
from edge_engine.scan import Engine, load_config

logging.basicConfig(level=logging.WARNING)
engine = Engine(load_config())
events = engine.fetch_events()

target = None
for ev in events:
    if ev.venue is Venue.POLYMARKET and "Cruz Azul" in ev.title:
        target = ev
        break
if target is None:
    for ev in events:
        if ev.venue is Venue.POLYMARKET and "Highest temperature in NYC" in ev.title:
            target = ev
            break

print(f"EVENT: {target.title}  negRisk={target.neg_risk} "
      f"legs={len(target.markets)} category={target.category}\n")

fetched = thin = failed = 0
for m in target.markets[:8]:
    tokens = engine.poly.token_ids(m.raw)
    book = engine._fetch_book(m)
    if book is None:
        failed += 1
        print(f"  {m.title[:34]:<36} FETCH FAILED (tokens={len(tokens)})")
        continue
    fetched += 1
    for side in (Side.YES, Side.NO):
        best = book.best_ask(side)
        depth10 = book.depth_available(side, (best or 0) + 0.02)
        fill = book.cost_to_fill(side, 100)
        status = "OK" if fill else "TOO THIN"
        if not fill:
            thin += 1
        print(f"  {m.title[:26]:<28} {side.value:<3} "
              f"best_ask={best if best is not None else 'None':<8} "
              f"depth@+2c={depth10:>10.1f}  fill(100)={status}")

print(f"\nfetched={fetched} failed={failed} thin_sides={thin}")
print("\nVERDICT:", "books fetch fine, depth is genuinely insufficient"
      if failed == 0 else "BOOK FETCHING IS BROKEN - investigate")
