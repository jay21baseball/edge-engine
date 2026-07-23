"""Confirm the pieces a live recorder needs actually exist:
   1. single-market price refetch on both venues (cheap, for tight polling)
   2. matchable live sports pairs across the two venues right now
"""
import json
import urllib.request
from collections import defaultdict

import sys
sys.path.insert(0, "src")
from edge_engine.ingest.kalshi import KalshiClient
from edge_engine.ingest.polymarket import PolymarketClient
from edge_engine.ingest.models import Venue
from edge_engine.strategies.sportsbook import teams_match, team_tokens


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


print("=" * 70)
print("1. SINGLE-MARKET REFETCH (needed for cheap 60s polling)")
print("=" * 70)

kalshi = KalshiClient()
poly = PolymarketClient()

# Grab one open Kalshi sports market ticker to test single fetch.
kev = kalshi.events(status="open", with_markets=True)
k_sports = [e for e in kev if "sport" in (e.category or "").lower()
            and e.markets]
print(f"Kalshi open sports events: {len(k_sports)}")
if k_sports:
    tk = k_sports[0].markets[0].market_id
    single = get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}")
    m = single.get("market", {})
    print(f"  single fetch {tk}: yes_bid={m.get('yes_bid_dollars')} "
          f"yes_ask={m.get('yes_ask_dollars')}  OK={'market' in single}")

pev = poly.events(limit=500, closed=False)
p_sports = [e for e in pev if "sport" in (e.category or "").lower()
            and e.markets]
print(f"Polymarket open sports events: {len(p_sports)}")
if p_sports:
    mid = p_sports[0].markets[0].market_id
    single = get(f"https://gamma-api.polymarket.com/markets/{mid}")
    ok = isinstance(single, dict) and "id" in single
    print(f"  single fetch {mid}: OK={ok} bestBid={single.get('bestBid')} "
          f"bestAsk={single.get('bestAsk')}")

print()
print("=" * 70)
print("2. MATCHABLE LIVE PAIRS RIGHT NOW")
print("=" * 70)

# Build team-token index for Kalshi sports markets.
def market_team(m):
    """Best guess at the team a YES backs, from the market title."""
    return m.title

k_markets = [(e, m) for e in k_sports for m in e.markets if m.status == "open"]
p_markets = [(e, m) for e in p_sports for m in e.markets if m.status == "open"]
print(f"Kalshi open sports markets: {len(k_markets)}")
print(f"Poly open sports markets:   {len(p_markets)}")

matches = 0
shown = 0
for ke, km in k_markets:
    for pe, pm in p_markets:
        kt = team_tokens(f"{ke.title} {km.title}")
        pt = team_tokens(f"{pe.title} {pm.title}")
        if not kt or not pt:
            continue
        # Both team names must appear in both event titles for a game match.
        if kt <= (team_tokens(pe.title) | pt) and len(kt & pt) >= 1:
            matches += 1
            if shown < 8:
                shown += 1
                print(f"  MATCH: K[{km.title[:34]}] <-> P[{pm.title[:34]}]  "
                      f"tokens={sorted(kt & pt)}")
print(f"\ncandidate cross-venue market matches: {matches}")
print("(noisy — real matcher will require aligned teams + same resolution day)")
