"""Does ESPN expose FREE live win probability? That is the sharp reference a
Polymarket live recorder would measure against. No key, no cost if it works."""
import json
import urllib.request


def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode())


LEAGUES = [
    ("baseball", "mlb", "MLB"),
    ("basketball", "wnba", "WNBA"),
    ("soccer", "usa.1", "MLS"),
]

for sport, league, label in LEAGUES:
    print("=" * 66)
    print(f"{label}  ({sport}/{league})")
    print("=" * 66)
    try:
        sb = get(f"https://site.api.espn.com/apis/site/v2/sports/{sport}/"
                 f"{league}/scoreboard")
    except Exception as e:
        print(f"  scoreboard failed: {e}")
        continue

    events = sb.get("events", [])
    live = [e for e in events
            if e.get("status", {}).get("type", {}).get("state") == "in"]
    print(f"  games today: {len(events)}   LIVE now: {len(live)}")

    sample = (live or events)[:1]
    for ev in sample:
        comp = ev.get("competitions", [{}])[0]
        teams = [c.get("team", {}).get("displayName")
                 for c in comp.get("competitors", [])]
        state = ev.get("status", {}).get("type", {}).get("state")
        print(f"  example: {' vs '.join(str(t) for t in teams)}  state={state}")
        eid = ev.get("id")
        try:
            summ = get(f"https://site.api.espn.com/apis/site/v2/sports/"
                       f"{sport}/{league}/summary?event={eid}")
            wp = summ.get("winprobability") or summ.get("winProbability")
            if wp:
                last = wp[-1]
                print(f"    WIN PROBABILITY present: {len(wp)} data points")
                print(f"    latest: {json.dumps(last)[:120]}")
            else:
                keys = list(summ.keys())
                print(f"    no winprobability key. summary keys: {keys[:12]}")
                # predictor is the pregame/live model on some sports
                pred = summ.get("predictor")
                if pred:
                    print(f"    predictor present: {json.dumps(pred)[:160]}")
        except Exception as e:
            print(f"    summary failed: {e}")
    print()
