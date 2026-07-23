"""What does a whale's live activity feed actually give us, per trade?
swisstony = 0x204f72f35326db932158cba6adff0b9a1da95e14"""
import json
import urllib.request
from datetime import datetime, timezone

W = "0x204f72f35326db932158cba6adff0b9a1da95e14"


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "probe"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


act = get(f"https://data-api.polymarket.com/activity?user={W}&limit=8")
print(f"activity rows: {len(act)}")
print("keys on a row:", sorted(act[0].keys()) if act else "none")
print()
now = datetime.now(timezone.utc)
for a in act[:8]:
    ts = datetime.fromtimestamp(a.get("timestamp", 0), tz=timezone.utc)
    mins = (now - ts).total_seconds() / 60
    print(f"  {a.get('type'):<6} {a.get('side',''):<4} "
          f"${a.get('usdcSize', 0):>10,.0f}  @ {a.get('price')}  "
          f"{mins:>6.0f}m ago  {str(a.get('title'))[:40]}")
    print(f"         outcome={a.get('outcome')!r} "
          f"size={a.get('size')} hash={str(a.get('transactionHash'))[:14]}")

# Is there a value/PnL summary endpoint for the header line?
try:
    val = get(f"https://data-api.polymarket.com/value?user={W}")
    print("\nvalue endpoint:", json.dumps(val)[:200])
except Exception as e:
    print("\nvalue endpoint failed:", e)
