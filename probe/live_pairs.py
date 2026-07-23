import sys
sys.path.insert(0, "src")
from edge_engine.scan import Engine, load_config
from edge_engine.ingest.espn import EspnClient
from edge_engine.strategies.live_recorder import build_pairings

e = Engine(load_config())
games = EspnClient().live_games(["mlb", "wnba", "nba", "nfl"])
evts = [ev for ev in e.poly.events(limit=400, closed=False)
        if "sport" in (ev.category or "").lower()]
pairs = build_pairings(games, evts)
print(f"LIVE PAIRINGS: {len(pairs)}")
for p in sorted(pairs, key=lambda x: -abs(x.net_gap)):
    print(f"  {p.team[:18]:<19} {p.game.detail[:11]:<12} "
          f"ESPN={p.espn_prob:.2f} POLY={p.poly_price:.2f}  "
          f"net={p.net_gap * 100:+5.1f}pt  liq=${p.market.liquidity:,.0f}")
