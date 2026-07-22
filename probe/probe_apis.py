"""Probe live public endpoints to capture real response shapes before coding against them."""
import json
import urllib.request
import urllib.error


def get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": "edge-engine-probe/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return {"__http_error__": e.code, "__body__": e.read().decode()[:400]}
    except Exception as e:
        return {"__error__": f"{type(e).__name__}: {e}"}


def shape(obj, depth=0, max_depth=2):
    """Summarize structure rather than dumping everything."""
    pad = "  " * depth
    if isinstance(obj, dict):
        if depth >= max_depth:
            return f"{{dict with {len(obj)} keys: {list(obj)[:14]}}}"
        lines = []
        for k, v in list(obj.items())[:30]:
            lines.append(f"{pad}{k}: {shape(v, depth + 1, max_depth)}")
        return "\n" + "\n".join(lines)
    if isinstance(obj, list):
        if not obj:
            return "[] (empty)"
        return f"[{len(obj)} items] first -> {shape(obj[0], depth + 1, max_depth)}"
    s = repr(obj)
    return s[:90] + ("..." if len(s) > 90 else "")


PROBES = [
    ("KALSHI markets",
     "https://api.elections.kalshi.com/trade-api/v2/markets?limit=2&status=open"),
    ("KALSHI events (with nested markets)",
     "https://api.elections.kalshi.com/trade-api/v2/events?limit=2&status=open&with_nested_markets=true"),
    ("POLY gamma markets",
     "https://gamma-api.polymarket.com/markets?limit=2&closed=false&order=volume24hr&ascending=false"),
    ("POLY gamma events (negRisk)",
     "https://gamma-api.polymarket.com/events?limit=2&closed=false&order=volume24hr&ascending=false"),
    ("POLY data leaderboard",
     "https://data-api.polymarket.com/v1/leaderboard?timePeriod=MONTH&orderBy=PNL&limit=3"),
]

for name, url in PROBES:
    print("=" * 78)
    print(f"### {name}")
    print(url)
    print("-" * 78)
    data = get(url)
    print(shape(data))
    print()
