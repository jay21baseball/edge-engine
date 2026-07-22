"""Does a closed/historical positions endpoint exist? /positions returns only live holdings."""
import json
import urllib.error
import urllib.request

WALLET = "0x204f72f35326db932158cba6adff0b9a1da95e14"  # leaderboard #1

CANDIDATES = [
    f"https://data-api.polymarket.com/closed-positions?user={WALLET}&limit=5",
    f"https://data-api.polymarket.com/v1/closed-positions?user={WALLET}&limit=5",
    f"https://data-api.polymarket.com/positions?user={WALLET}&limit=5&closed=true",
    f"https://data-api.polymarket.com/positions?user={WALLET}&limit=5&redeemable=true",
    f"https://data-api.polymarket.com/trades?user={WALLET}&limit=5",
]

for url in CANDIDATES:
    label = url.split("data-api.polymarket.com/")[1][:60]
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "probe"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode())
        n = len(data) if isinstance(data, list) else "dict"
        keys = sorted(data[0].keys())[:16] if isinstance(data, list) and data else []
        print(f"OK   {label}\n     n={n} keys={keys}\n")
    except urllib.error.HTTPError as e:
        print(f"FAIL {label} -> HTTP {e.code}")
    except Exception as e:
        print(f"FAIL {label} -> {type(e).__name__}")
