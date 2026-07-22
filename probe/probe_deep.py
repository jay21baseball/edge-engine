"""Second probe: structures needed for combinatorial arb + the MM-detection hypothesis."""
import json
import urllib.request
import urllib.error


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "edge-engine-probe/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "__body__": e.read().decode()[:300]}
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


print("=" * 78)
print("### A. Leaderboard: volume-to-PnL ratio across top 25 (MM-detection hypothesis)")
print("=" * 78)
lb = get("https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=25")
if isinstance(lb, list):
    print(f"{'rank':>4} {'user':<22} {'pnl':>12} {'volume':>14} {'vol/pnl':>8}  profile")
    for e in lb:
        pnl, vol = e.get("pnl") or 0, e.get("vol") or 0
        ratio = (vol / pnl) if pnl > 0 else float("inf")
        tag = "LIKELY MM" if ratio > 20 else ("high-turnover" if ratio > 8 else "directional?")
        print(f"{e.get('rank'):>4} {(e.get('userName') or '')[:22]:<22} "
              f"{pnl:>12,.0f} {vol:>14,.0f} {ratio:>8.1f}  {tag}")
else:
    print(lb)

print()
print("=" * 78)
print("### B. Polymarket negRisk event (multi-outcome mutually exclusive)")
print("=" * 78)
evs = get("https://gamma-api.polymarket.com/events?limit=40&closed=false&active=true"
          "&order=volume24hr&ascending=false")
neg = None
if isinstance(evs, list):
    for e in evs:
        if e.get("negRisk") and len(e.get("markets") or []) >= 3:
            neg = e
            break
    print(f"scanned {len(evs)} events; negRisk flag present on event objects: "
          f"{any('negRisk' in e for e in evs)}")
if neg:
    print(f"\nTITLE: {neg.get('title')}")
    print(f"negRisk={neg.get('negRisk')}  negRiskMarketID={neg.get('negRiskMarketID')}")
    print(f"markets: {len(neg['markets'])}")
    total_yes = 0.0
    for m in neg["markets"][:12]:
        try:
            prices = json.loads(m.get("outcomePrices") or "[]")
            outs = json.loads(m.get("outcomes") or "[]")
        except Exception:
            prices, outs = [], []
        yes = float(prices[0]) if prices else 0.0
        total_yes += yes
        print(f"   {m.get('groupItemTitle') or m.get('question','')[:48]:<50} "
              f"outcomes={outs} prices={prices}")
    print(f"\n   >>> SUM OF YES PRICES = {total_yes:.4f}   "
          f"({'ARB (sum<1)' if total_yes < 1 else 'no arb at mid'})")
    print(f"   market keys sample: {sorted(neg['markets'][0].keys())}")

print()
print("=" * 78)
print("### C. Kalshi mutually_exclusive event")
print("=" * 78)
kev = get("https://api.elections.kalshi.com/trade-api/v2/events?limit=60&status=open"
          "&with_nested_markets=true")
found = None
if isinstance(kev, dict) and kev.get("events"):
    for e in kev["events"]:
        if e.get("mutually_exclusive") and len(e.get("markets") or []) >= 3:
            found = e
            break
    print(f"scanned {len(kev['events'])} events; "
          f"{sum(1 for e in kev['events'] if e.get('mutually_exclusive'))} mutually_exclusive")
if found:
    print(f"\nTITLE: {found.get('title')}  ({found.get('event_ticker')})")
    print(f"collateral_return_type={found.get('collateral_return_type')}")
    tot = 0.0
    for m in found["markets"][:12]:
        ya = m.get("yes_ask_dollars") or m.get("yes_ask")
        yb = m.get("yes_bid_dollars") or m.get("yes_bid")
        tot += float(ya or 0)
        print(f"   {(m.get('yes_sub_title') or m.get('ticker',''))[:44]:<46} "
              f"yes_bid={yb} yes_ask={ya}")
    print(f"\n   >>> SUM OF YES ASKS = {tot:.4f}")
    print(f"   market keys: {sorted(found['markets'][0].keys())}")

print()
print("=" * 78)
print("### D. Order books")
print("=" * 78)
if found:
    tk = found["markets"][0]["ticker"]
    ob = get(f"https://api.elections.kalshi.com/trade-api/v2/markets/{tk}/orderbook?depth=4")
    print(f"KALSHI {tk}: {json.dumps(ob)[:420]}")
if neg:
    try:
        tid = json.loads(neg["markets"][0].get("clobTokenIds") or "[]")
        if tid:
            bk = get(f"https://clob.polymarket.com/book?token_id={tid[0]}")
            print(f"\nPOLY token {tid[0][:22]}...:")
            print(f"   keys={list(bk) if isinstance(bk, dict) else bk}")
            if isinstance(bk, dict):
                print(f"   bids[:3]={ (bk.get('bids') or [])[:3] }")
                print(f"   asks[:3]={ (bk.get('asks') or [])[:3] }")
    except Exception as ex:
        print(f"poly book err: {ex}")

print()
print("=" * 78)
print("### E. Wallet activity/positions shape (top trader)")
print("=" * 78)
if isinstance(lb, list) and lb:
    w = lb[0]["proxyWallet"]
    pos = get(f"https://data-api.polymarket.com/positions?user={w}&limit=2")
    act = get(f"https://data-api.polymarket.com/activity?user={w}&limit=3")
    print(f"wallet {w}")
    print(f"positions -> {json.dumps(pos)[:600] if pos else pos}")
    print(f"\nactivity  -> {json.dumps(act)[:600] if act else act}")
