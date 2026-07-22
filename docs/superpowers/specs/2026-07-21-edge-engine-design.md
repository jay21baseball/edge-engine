# edge-engine — Design Spec

**Date:** 2026-07-21
**Status:** Approved for M1 implementation
**Owner:** Jay

---

## 1. Purpose and boundary

A local-first research and discipline system for Kalshi and Polymarket. It ingests
public market and wallet data, scores opportunities, sizes them against a configured
bankroll, alerts via Telegram, and grades its own forecast accuracy over time.

**It does not place orders.** Every signal produces a filled-in order ticket
(venue, market, side, price, stake, rationale) that the operator executes by hand.
This is a deliberate constraint: a small bankroll dies faster to an automation bug
than to bad selection.

**It is not investment advice.** The system computes and displays arithmetic. Trade
decisions belong to the operator.

### Operating constraints that drive every decision

| Constraint | Value | Consequence |
|---|---|---|
| Starting bankroll | $2,500 | Capital velocity dominates edge size |
| Time budget | Always-on scanner, alert-driven | Scheduled scans, push notification |
| Execution | Manual, two venues | Leg risk is the dominant blow-up mode |
| Platform | Windows, Python 3.12 | Local process, SQLite, Task Scheduler |

**The binding constraint is not finding edge. It is capital velocity and not blowing
up.** A 3% edge resolving in 2 days recycles ~180x/year. An 8% edge resolving in 8
months recycles 1.5x. The "smaller" edge is worth ~100x more annually. Every ranking
decision in this system flows from that.

---

## 2. Verified findings (live API probe, 2026-07-21)

These were confirmed against production endpoints, not documentation.

### 2.1 Fee formulas are the same shape, different rates

Both venues charge a parabolic fee peaking at 50c.

```
Kalshi:      fee = ceil(0.07 * p * (1-p) * 100) / 100     per contract  [ROUNDS UP TO CENT]
Polymarket:  fee = C * feeRate * p * (1-p)                 [rounds to 5dp, no penalty]
```

Polymarket `feeRate` by category:

| Category | Rate | vs Kalshi 0.07 |
|---|---|---|
| Geopolitics | **0.00** | free |
| Politics, Finance, Tech, Mentions | 0.04 | 43% cheaper |
| Sports, Economics, Culture, Weather, Other | 0.05 | 29% cheaper |
| Crypto | 0.07 | equal |

**Consequence: venue routing is an edge on every trade.** Where the same event trades
on both venues, fee-aware routing is free money that compounds. Build it into core.

**Consequence: Kalshi's ceil-to-cent breaks multi-leg arb.** Each leg costs a full
cent minimum regardless of contract price. An 8-leg arb pays $0.08 in fees before
any gross profit exists.

Live example found during probe — Kalshi `KXNEXTNATOSECGEN-99`, 8 candidates:

```
Sum of YES asks:  $0.98   -> looks like 2.04% risk-free
Kalshi fees:      $0.08   (8 legs, ceil-to-cent floor)
NET:             -$0.06   -> a 6% LOSS
```

A naive scanner reports this as free money. This single case justifies the entire
fee-aware design.

**Consequence: combinatorial arb belongs primarily on Polymarket negRisk events**,
and a geopolitics negRisk event with `sum(YES asks) < 1` is the highest-value target
in the system: mechanically enforced, zero fee drag.

### 2.2 negRisk makes combinatorial arb mechanically enforced

Polymarket's neg-risk CTF adapter converts a NO share in any market of a mutually
exclusive set into a YES share in every other market. For an n-question set,
converting m NO tokens returns `m-1` collateral plus YES in the remainder. The arb
condition is therefore a hard on-chain identity, not a hope that both legs fill.

Detection: `negRisk == true` on the Gamma event object, plus `negRiskMarketID`.
Never infer mutual exclusivity from titles.

### 2.3 Kalshi order books are bids-only for both sides

`GET /markets/{ticker}/orderbook` returns `orderbook_fp.yes_dollars` and
`no_dollars`, each `[[price, size], ...]`. **Both are bid ladders.** The YES ask is
derived:

```
yes_ask = 1 - best_no_bid
```

Verified: market showed `yes_bid=0.08, yes_ask=0.14`; book had best `no_dollars` bid
at `0.86`; `1 - 0.86 = 0.14`. A scanner that reads `yes_dollars` as asks computes
prices that are catastrophically wrong.

### 2.4 The leaderboard is dominated by uncopyable flow

Top-25 by monthly P&L, volume-to-P&L ratio:

| Rank | User | P&L | Volume | Vol:P&L | Classification |
|---|---|---|---|---|---|
| 1 | swisstony | $8.56M | $376M | 44:1 | market maker |
| 3 | asparagus2012 | $3.66M | $2.42M | 0.7:1 | concentration/luck |
| 9 | BreakTheBank | $2.30M | $81M | 35:1 | market maker |
| 10 | 0x2c33... | $2.08M | $323M | 155:1 | market maker |
| 25 | RN1 | $1.15M | $152M | 132:1 | market maker |

Roughly half the top 25 exceed 8:1. Wallet #1's open positions are Brazilian
football and ATP tennis at high frequency, including one at -99.9%. That is
inventory, not conviction.

A ratio below ~1:1 (rank 3) indicates P&L concentrated in few longshot wins —
variance, not skill.

**Naive leaderboard copying mirrors market makers and lottery winners
simultaneously.** Both failure modes must be screened out explicitly.

### 2.5 Confirmed endpoints (all public, no auth)

| Purpose | Endpoint |
|---|---|
| Kalshi markets | `https://api.elections.kalshi.com/trade-api/v2/markets` |
| Kalshi events (+`mutually_exclusive`) | `.../events?with_nested_markets=true` |
| Kalshi book | `.../markets/{ticker}/orderbook?depth=N` |
| Poly markets | `https://gamma-api.polymarket.com/markets` |
| Poly events (+`negRisk`) | `https://gamma-api.polymarket.com/events` |
| Poly book | `https://clob.polymarket.com/book?token_id=` |
| Poly leaderboard | `https://data-api.polymarket.com/v1/leaderboard` |
| Poly positions | `https://data-api.polymarket.com/positions?user=` |
| Poly activity | `https://data-api.polymarket.com/activity?user=` |

Note: the documented Kalshi hosts (`trading-api.kalshi.com`,
`external-api.kalshi.com`) are stale. `api.elections.kalshi.com` is live.

Polymarket quirks: `outcomes` and `outcomePrices` are **JSON-encoded strings**, not
arrays. `orderMinSize` is typically 5 shares. Per-market fee fields (`takerBaseFee`,
`makerBaseFee`, `feeSchedule`, `feesEnabled`) exist — read live rather than hardcode.

---

## 3. Architecture

```
edge-engine/
  config.yaml               bankroll, thresholds, strategy gates
  src/edge_engine/
    ingest/
      kalshi.py             client + normalization
      polymarket.py         client + normalization
      models.py             Market, Event, OrderBook, Wallet dataclasses
    store/
      base.py               Storage interface   <- the deploy seam
      sqlite_store.py       local implementation
      schema.sql
    strategies/
      base.py               Strategy protocol -> emits Signal
      combinatorial.py      [M1] within-venue mutually-exclusive arb
      wallet_signal.py      [M1] qualified-wallet attention queue
      cross_venue.py        [M1 observe-only] log spreads, never alert
      weather.py            [M2] NOAA/NWS vs Kalshi implied
      favorite_longshot.py  [M2] fitted bias correction
    sizing/
      fees.py               exact per-venue fee math
      kelly.py              fractional Kelly
      bankroll.py           tier gating, exposure caps
      discipline.py         trade caps, drawdown circuit breaker
    journal/
      log.py                every signal recorded at issue
      calibration.py        Brier score, reliability curve
    alert/
      telegram.py           push
      briefing.py           daily digest, sectioned by horizon
    scan.py                 scheduler loop
```

Each strategy is independent and emits a common `Signal`. Adding one later touches
no other module.

**Snapshot every market on every scan.** Storage is cheap; within weeks this becomes
a proprietary historical series for both venues that enables backtesting and
calibration. Highest-return decision in the project.

---

## 4. Data model (SQLite)

| Table | Purpose |
|---|---|
| `markets` | venue, market_id, event_id, title, category, close_ts, fee_rate, status |
| `market_snapshots` | market_id, ts, yes_bid, yes_ask, no_bid, no_ask, volume, liquidity |
| `events` | venue, event_id, title, mutually_exclusive, neg_risk, category |
| `order_books` | market_id, ts, side, levels_json (only for arb candidates) |
| `wallets` | address, username, first_seen, last_seen |
| `wallet_snapshots` | address, ts, window, category, rank, pnl, volume |
| `wallet_positions` | address, market_id, side, size, avg_price, ts_observed |
| `wallet_scores` | address, ts, n_resolved, entry_adj_edge, brier, herfindahl, mm_score, qualified |
| `signals` | ts, strategy, venue, market_id, side, entry_price, est_prob, edge, confidence, days_to_resolve, score, rationale_json |
| `journal` | signal_id, alerted_ts, taken, actual_entry, stake, outcome, pnl, resolved_ts |
| `cross_venue_log` | ts, kalshi_id, poly_id, spread, depth, net_after_fees, persisted_seconds |

---

## 5. M1 scope

### 5.1 Combinatorial arb scanner

For a mutually-exclusive set of n markets:

```
sum(yes_ask) < 1 - fees            -> buy every YES
sum(no_ask)  < (n-1) - fees        -> buy every NO
yes_ask + no_ask < 1 - fees        -> single binary, buy both sides
```

Requirements:

1. **Walk the order book at intended fill size.** Never mid, never last price. Depth
   is public on both venues. Phantom arbs that exist for 3 contracts at top-of-book
   are the primary false positive.
2. **Derive Kalshi asks correctly** (`1 - best_no_bid`), per 2.3.
3. **Confirm mutual exclusivity from the API flag** (`negRisk` / `mutually_exclusive`),
   never from title similarity.
4. **Net exact per-venue fees** including Kalshi's ceil-to-cent, per 2.1.
5. **Verify all legs resolve on the same date.** Mismatched resolution is timing
   risk, not arbitrage.
6. **Re-verify the book immediately before alerting.** If the spread evaporated, drop
   silently.

Priority ordering: Polymarket geopolitics negRisk (zero fees) > other Polymarket
negRisk > Kalshi with few legs. Kalshi multi-leg is deprioritized by fee math.

### 5.2 Wallet skill screen

**Discovery:** leaderboard across 4 time windows x ~9 categories x 50 results
(~1,800 slots), deduped to unique addresses.

**Enrichment:** `/positions`, `/activity` per wallet, throttled (positions endpoint
limit is 150 req/10s).

**Screens — all must pass to qualify:**

| Screen | Rule | Rationale |
|---|---|---|
| Sample size | >= 30 resolved positions | Kills lucky-whale selection at source |
| Entry-adjusted edge | mean(`outcome - avg_price`) significantly > 0 | Buying 90c favorites and winning 90% is **zero skill**. The public leaderboard misses this entirely. |
| Brier score | below threshold | Calibration of implied forecasts |
| P&L Herfindahl | no single trade > 40% of lifetime P&L | Rank 3 pattern: lottery ticket, not process |
| MM detection | **hard exclude** if vol:P&L > 20, or two-sided flow in same market, or short median hold | Their position is inventory they are paying to shed |
| Recency | exponential decay, 90-day half-life | Sharp on 2024 elections != sharp now |
| Category | scored per category, not globally | Great at sports != signal in crypto |

**Signal generation:** market surfaces when **>= 2 qualified wallets** (in that
category) hold the same side within a recency window.

**Every alert must display decayed edge:** "they entered at 41c, now 47c — you are
getting 68% of their edge." That number is usually the reason not to take the trade
and must be shown every time.

This is an **attention queue, not a copy signal.** It reduces 10,000+ markets to a
short list for human evaluation.

### 5.3 Cross-venue: observe-only

The scanner runs and logs every spread found — size, depth available, seconds
persisted — but **never alerts and never sizes.** Rationale below.

---

## 6. Bankroll and discipline layer

```yaml
bankroll: 2500
kelly_fraction: 0.25
max_single_position_pct: 5
max_concurrent_exposure_pct: 40
min_edge_threshold_pct: 4          # scales DOWN as bankroll grows
max_trades_per_day: 3
drawdown_circuit_breaker_pct: 15
```

Everything derives from `bankroll`. One number changes and unit sizes, thresholds,
and caps all move.

**Strategy gating by tier, auto-unlocking:**

| Strategy | Minimum bankroll |
|---|---|
| Combinatorial arb, wallet-filtered discretionary | $0 |
| Cross-venue arb | $15,000 |
| Passive liquidity provision | $50,000 |

Kelly: `f* = (p(b+1) - 1) / b` where `b = (1-price)/price`, then x `kelly_fraction`,
then clamped to `max_single_position_pct`. Quarter-Kelly because full Kelly assumes
you *know* p; you are estimating it, and Kelly punishes optimistic estimates
severely.

**Circuit breaker:** down `drawdown_circuit_breaker_pct` on the week and the system
stops issuing buy alerts and reports a stand-down.

### Why cross-venue is gated at $15,000

Worked at $2,500 split $1,250/$1,250 across venues:

- A 3c gross spread nets ~$0.001/pair after fees — a rounding error.
- A genuine 6c spread nets ~3.3%, but exists at only 50-200 contracts of depth.
  Realistic fill $100-300 -> **$3-10 profit per opportunity.**
- **Leg risk dominates.** Manual execution across two venues means filling one side
  and watching the other move. One blown leg at $2,500 is a **5-10% bankroll hit**,
  erasing ~20 successful arbs.
- **Fragmentation:** splitting capital halves the single-venue strategies that are
  actually executable.
- **Rebalancing friction:** ACH out of Kalshi is 1-3 days; near-fixed transfer costs
  are a large percentage of a small stack.

At $15,000 a blown leg is 1-2% — an annoyance rather than a catastrophe. The gate is
a judgment call with a rationale, not a derived constant, which is why the observe-
only log exists: after ~3 months the operator will have empirical data on whether
cross-venue arb at their scale was ever real. Flipping the gate is one config line.

---

## 7. Self-calibration — the gate on scaling

Every signal is logged at issue time with its claimed probability, **whether or not
it is taken.** On resolution, signals are bucketed by predicted probability and
compared to realized frequency, producing a reliability curve and Brier score.

**Scaling rule: do not increase deployed bankroll until >= 100 resolved signals show
calibration within tolerance.** If the system says 70% and those resolve at 52%, that
is discovered on paper rather than with money.

Logging untaken alerts separates "the model is bad" from "the model is fine and
execution is bad" — opposite problems with opposite fixes.

---

## 8. Failure modes and mitigations

| Failure mode | Mitigation |
|---|---|
| Phantom arb from top-of-book | Walk book at fill size; re-verify before alert |
| Kalshi ask misread | Derive `1 - best_no_bid`; unit tested |
| Fee math error inverts sign | Exact formulas, unit tested against published examples |
| Non-identical markets matched cross-venue | **Explicit human confirmation required** before any pair can alert, ever |
| Stale data | Snapshots older than N minutes barred from generating alerts |
| Schema drift | Pydantic validation; loud alert on unexpected shape, never silent misread |
| Rate limiting | Token bucket per venue; wallet refresh throttled |
| Resolution-date mismatch in arb legs | All legs must share a resolution date |
| Operator override of circuit breaker | Breaker state shown in every alert |

---

## 9. Testing

- **Fee formulas** — unit tested against published examples for both venues,
  including Kalshi ceil-to-cent behaviour. Everything downstream depends on these.
- **Kelly sizing** — including `p <= price` returning zero stake, never negative.
- **Arb detection** — hand-built books including one that looks arb'd on mids but is
  not on asks, and one where fees invert the sign (the NATO case from 2.1).
- **Kalshi ask derivation** — against the recorded live book.
- **MM detection** — against the recorded top-25 leaderboard profile.
- **Golden-file tests** on recorded API responses to catch schema drift.
- **Backtest harness** replaying stored snapshots through strategies.

---

## 10. Deferred to later milestones

| Milestone | Contents |
|---|---|
| M2 | Weather model (NOAA/NWS ensemble vs Kalshi implied); favorite-longshot correction fitted on collected snapshots |
| M3 | Cross-venue matching with human confirmation UI; enable arb at bankroll gate |
| M4 | Cloud promotion via the storage seam; hosted dashboard |
| M5 | Passive liquidity provision (requires $50k tier) |

M2 strategies are deliberately gated behind M1's journal producing enough resolved
signals to validate them. Shipping a forecasting model before you can measure its
calibration is how these systems lose money confidently.
