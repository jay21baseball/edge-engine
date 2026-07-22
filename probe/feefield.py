"""What is actually in Polymarket's takerBaseFee / makerBaseFee / feeSchedule?"""
import json
import urllib.request
from collections import Counter

req = urllib.request.Request(
    "https://gamma-api.polymarket.com/events?limit=60&closed=false&active=true"
    "&order=volume24hr&ascending=false",
    headers={"User-Agent": "probe"},
)
events = json.loads(urllib.request.urlopen(req, timeout=30).read().decode())

vals = Counter()
samples = []
for ev in events:
    for m in (ev.get("markets") or []):
        tb = m.get("takerBaseFee")
        mb = m.get("makerBaseFee")
        vals[(repr(tb), repr(mb), repr(m.get("feeType")))] += 1
        if len(samples) < 6:
            samples.append({
                "event": ev.get("title", "")[:40],
                "takerBaseFee": tb, "makerBaseFee": mb,
                "feeType": m.get("feeType"),
                "feesEnabled": m.get("feesEnabled"),
                "feeSchedule": m.get("feeSchedule"),
                "tags": [t.get("label") for t in (ev.get("tags") or [])][:4],
            })

print("=== distinct (takerBaseFee, makerBaseFee, feeType) values ===")
for k, v in vals.most_common(15):
    print(f"  {v:>5}x  taker={k[0]:<10} maker={k[1]:<10} type={k[2]}")

print("\n=== samples ===")
for s in samples:
    print(json.dumps(s, indent=2)[:500])
