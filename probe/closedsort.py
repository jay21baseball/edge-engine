"""Is /closed-positions sorted by pnl, and can we get an unbiased sample?"""
import json
import urllib.parse
import urllib.request

WALLET = "0x204f72f35326db932158cba6adff0b9a1da95e14"


def get(**params):
    url = ("https://data-api.polymarket.com/closed-positions?"
           + urllib.parse.urlencode({"user": WALLET, **params}))
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        return {"err": str(e)[:90]}


base = get(limit=500)
print(f"limit=500 -> {len(base) if isinstance(base, list) else base} rows")
if isinstance(base, list) and base:
    pnls = [b.get("realizedPnl") for b in base]
    print(f"  first 6 realizedPnl: {[round(p, 1) for p in pnls[:6]]}")
    print(f"  last  6 realizedPnl: {[round(p, 1) for p in pnls[-6:]]}")
    print(f"  descending by pnl? {pnls == sorted(pnls, reverse=True)}")
    wins = sum(1 for b in base if (b.get('realizedPnl') or 0) > 0)
    print(f"  winners: {wins}/{len(base)}  <-- 100% means winners-only sample")

print()
for extra in ({"offset": 50}, {"offset": 200},
              {"sortBy": "TIMESTAMP"}, {"sortDirection": "ASC"},
              {"sortBy": "TIMESTAMP", "sortDirection": "ASC"}):
    r = get(limit=50, **extra)
    if isinstance(r, list) and r:
        pnls = [round(x.get("realizedPnl") or 0, 1) for x in r[:4]]
        wins = sum(1 for x in r if (x.get('realizedPnl') or 0) > 0)
        print(f"{str(extra):<46} n={len(r):<4} first_pnl={pnls} wins={wins}/{len(r)}")
    else:
        print(f"{str(extra):<46} -> {r if not isinstance(r, list) else 'empty'}")
