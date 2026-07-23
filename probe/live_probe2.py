"""Do the SAME games trade live on both venues today? That is the whole
premise of a cross-venue live recorder — verify it before building one."""
import sys
from datetime import datetime, timezone, timedelta
sys.path.insert(0, "src")

from edge_engine.ingest.kalshi import KalshiClient
from edge_engine.ingest.polymarket import PolymarketClient

now = datetime.now(timezone.utc)
soon = now + timedelta(hours=8)


def imminent(m):
    return m.close_ts is not None and now <= m.close_ts <= soon


kalshi = KalshiClient()
poly = PolymarketClient()

kev = kalshi.events(status="open", with_markets=True)
pev = poly.events(limit=600, closed=False)

# Kalshi: markets resolving in the next 8h, grouped by event.
k_soon = {}
for e in kev:
    live = [m for m in e.markets if imminent(m)]
    if live:
        k_soon[e.title] = (e.category, live)

p_soon = {}
for e in pev:
    live = [m for m in e.markets if imminent(m)]
    if live:
        p_soon[e.title] = (e.category, live)

print(f"Kalshi events resolving < 8h:     {len(k_soon)}")
print(f"Polymarket events resolving < 8h: {len(p_soon)}")

print("\n--- KALSHI, next 8h (first 25) ---")
for title, (cat, mks) in list(k_soon.items())[:25]:
    print(f"  [{cat[:10]:<10}] {title[:60]}")

print("\n--- POLYMARKET, next 8h (first 25) ---")
for title, (cat, mks) in list(p_soon.items())[:25]:
    print(f"  [{cat[:10]:<10}] {title[:60]}")

# Naive overlap: any shared distinctive word between a Kalshi and Poly title.
STOP = {"will", "the", "vs", "on", "at", "in", "be", "to", "a", "of", "and",
        "win", "by", "for", "2026", "game", "match", "who"}


def words(t):
    return {w for w in t.lower().replace("?", " ").replace(".", " ").split()
            if len(w) > 3 and w not in STOP}


print("\n--- POSSIBLE SAME-GAME OVERLAPS ---")
hits = 0
for kt in k_soon:
    for pt in p_soon:
        shared = words(kt) & words(pt)
        if len(shared) >= 2:
            hits += 1
            if hits <= 15:
                print(f"  K: {kt[:44]}")
                print(f"  P: {pt[:44]}   shared={sorted(shared)}")
                print()
print(f"total title overlaps (>=2 shared words): {hits}")
