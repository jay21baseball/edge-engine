"""Why is entry-adjusted edge still implausibly high? Inspect raw closed positions."""
import json
import urllib.parse
import urllib.request
from collections import Counter


def get(wallet, **params):
    url = ("https://data-api.polymarket.com/closed-positions?"
           + urllib.parse.urlencode({"user": wallet, "limit": 50,
                                     "sortBy": "TIMESTAMP", **params}))
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


# cumulus33, scored at +0.461
WALLET = None
lb = json.loads(urllib.request.urlopen(urllib.request.Request(
    "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&limit=50",
    headers={"User-Agent": "probe"}), timeout=25).read().decode())
for e in lb:
    if e.get("userName") == "cumulus33":
        WALLET = e["proxyWallet"]
if WALLET is None:
    WALLET = lb[3]["proxyWallet"]

rows = get(WALLET)
print(f"wallet {WALLET}  rows={len(rows)}\n")

cur = Counter()
for r in rows:
    c = r.get("curPrice")
    cur[("0" if c == 0 else "1" if c == 1 else "mid")] += 1
print(f"curPrice distribution: {dict(cur)}")

print(f"\n{'avgPrice':>9} {'curPrice':>9} {'realizedPnl':>12} {'size':>10}  "
      f"{'outcome':<6} title")
for r in rows[:14]:
    print(f"{r.get('avgPrice'):>9} {r.get('curPrice'):>9} "
          f"{round(r.get('realizedPnl') or 0, 2):>12} "
          f"{round(r.get('size') or 0, 1):>10}  "
          f"{str(r.get('outcome'))[:5]:<6} {str(r.get('title'))[:44]}")

wins = sum(1 for r in rows if (r.get("realizedPnl") or 0) > 0)
naive = [(1.0 if (r.get("curPrice") or 0) >= 0.5 else 0.0) - (r.get("avgPrice") or 0)
         for r in rows if 0 < (r.get("avgPrice") or 0) < 1]
print(f"\nwinners by realizedPnl: {wins}/{len(rows)}")
print(f"naive edge (curPrice>=0.5 as outcome): {sum(naive)/len(naive):+.4f}")

sizes = [r.get("size") or 0 for r in rows]
print(f"size: min={min(sizes)} max={max(sizes)} zero_count={sizes.count(0)}")
