"""Which screen is binding? Distinguishes "nobody qualifies" from "screen is broken"."""
import logging
from collections import Counter

from edge_engine.ingest.polymarket import PolymarketClient
from edge_engine.strategies.wallet_signal import score_wallet

logging.basicConfig(level=logging.ERROR)
poly = PolymarketClient()

discovered = poly.discover_wallets(windows=("MONTH", "ALL"),
                                   categories=("OVERALL", "SPORTS", "POLITICS"))
ranked = sorted(discovered.values(), key=lambda e: e.volume_to_pnl)[:40]

reasons = Counter()
resolved_counts = []
pos_counts = []

for entry in ranked:
    try:
        positions = poly.positions(entry.address)
        activity = poly.activity(entry.address)
    except Exception:
        continue
    s = score_wallet(entry.address, entry, positions, activity)
    pos_counts.append(len(positions))
    resolved_counts.append(s.n_resolved)
    for r in s.disqualified_for:
        reasons[r.split("(")[0].split(" below")[0].split(" exceeds")[0].strip()] += 1

print("===== DISQUALIFICATION REASONS (40 wallets) =====")
for reason, count in reasons.most_common():
    print(f"  {count:>3}x  {reason}")

print(f"\npositions returned per wallet: "
      f"min={min(pos_counts)} median={sorted(pos_counts)[len(pos_counts)//2]} "
      f"max={max(pos_counts)}")
print(f"RESOLVED detected per wallet:  "
      f"min={min(resolved_counts)} "
      f"median={sorted(resolved_counts)[len(resolved_counts)//2]} "
      f"max={max(resolved_counts)}")

print("\n===== SAMPLE WALLET DETAIL =====")
for entry in ranked[:3]:
    positions = poly.positions(entry.address)
    activity = poly.activity(entry.address)
    s = score_wallet(entry.address, entry, positions, activity)
    print(f"\n{s.username or s.address[:14]}  vol:pnl={s.volume_to_pnl:.1f}")
    print(f"  positions={len(positions)} resolved={s.n_resolved} "
          f"edge={s.entry_adjusted_edge:+.4f} t={s.t_stat:.2f} "
          f"brier={s.brier:.3f} herf={s.pnl_herfindahl:.3f}")
    for r in s.disqualified_for:
        print(f"    - {r}")
    redeemable = sum(1 for p in positions if p.redeemable)
    extreme = sum(1 for p in positions if p.current_price <= 0.02
                  or p.current_price >= 0.98)
    print(f"  redeemable={redeemable} extreme_price={extreme}")
