# edge-engine

A local-first research and discipline system for Kalshi and Polymarket.

It ingests public market and wallet data from both venues, finds opportunities
that survive real fees and real order-book depth, sizes them against your
bankroll, alerts you on Telegram, and grades its own accuracy over time.

**It does not place orders.** You get a filled-in order ticket; you click the
button. Nothing here is investment advice.

---

## Quick start

```bash
pip install pytest pyyaml
```

```bash
python -m edge_engine.scan status
```

```bash
python -m edge_engine.scan scan
```

```bash
python -m edge_engine.scan wallets
```

```bash
python -m edge_engine.scan watch
```

Set `PYTHONPATH=src` (or `pip install -e .`). No API keys are required for
anything above - every endpoint used is public.

Telegram is optional. Leave the tokens `null` in `config.yaml` and alerts print
to the console. To enable it: create a bot via **@BotFather**, message it once,
then read your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

---

## What it actually does

### Combinatorial arbitrage (deterministic)

For a mutually exclusive set of `n` markets, exactly one resolves YES:

```
buy every YES:  pay sum(yes_ask), receive $1      -> edge = 1 - sum(yes_ask)
buy every NO:   pay sum(no_ask),  receive $(n-1)  -> edge = (n-1) - sum(no_ask)
```

Arithmetic, not forecasting. The hard part is refusing to believe your own
scanner. Two phases:

1. **Screen** on quoted prices (free) and reject anything that cannot clear the
   fee burden.
2. **Verify** survivors against real order books walked at real fill size. A
   partial fill is not a smaller arb - it is a naked directional position.

### Wallet attention queue (not copy trading)

The public leaderboard ranks by absolute P&L, which selects for bankroll size and
variance rather than skill. Wallets are re-scored on:

| Screen | Rule |
|---|---|
| Sample size | >= 30 resolved positions |
| **Entry-adjusted edge** | `mean(outcome - avg_price) > 0` |
| Brier score | calibration of implied forecasts |
| P&L concentration | no single trade > 40% of lifetime P&L |
| **Market-maker detection** | hard exclude on volume:P&L > 20, two-sided flow, round-tripping |
| Recency | 90-day exponential half-life |
| Category | scored per category, not globally |

Entry-adjusted edge is the metric that matters: a trader who buys 90c favorites
and wins 90% of the time has **zero skill** - they are paying exactly fair value.
The public leaderboard measures none of this.

Markets surface when >= 2 qualified wallets agree, and every alert states how
much of the move already happened. That number is usually the reason not to take
the trade.

### Bankroll and discipline

Everything derives from one number in `config.yaml`. Change `bankroll` and unit
sizes, edge floors, trade caps and strategy availability all move with it.

| Strategy | Unlocks at |
|---|---|
| Combinatorial arb, wallet attention | $0 |
| Cross-venue arb | $15,000 |
| Passive liquidity provision | $50,000 |

Quarter-Kelly by default. A weekly drawdown circuit breaker halts all buy alerts.
Daily trade cap of 3.

**The edge floor rises as bankroll falls.** Counterintuitive but correct: fixed
costs are a larger fraction of a small stake and there is less cushion for
variance. Five high-edge trades a week beats thirty marginal ones.

### Self-calibration

Every signal is logged at issue time with its claimed probability, taken or not.
On resolution, predictions are bucketed and compared to realized frequency.

**Do not increase deployed bankroll until >= 100 resolved signals show
calibration within tolerance.** Logging untaken alerts separates "the model is
bad" from "the model is fine and my execution is bad" - opposite problems.

---

## Findings from building it

Verified against live APIs on 2026-07-21, not documentation.

**Both venues use the same parabolic fee shape, but different rates.**
Kalshi is `ceil(0.07 * C * p * (1-p) * 100)/100`. Polymarket is
`C * rate * p * (1-p)` with rate varying by category - `0.04` politics/finance/
tech, `0.05` sports/economics/weather, `0.07` crypto, and **`0.00` geopolitics**.
Fee-aware venue routing is free money on every trade.

**Multi-leg arb fee burden converges on the full fee rate.** For `n` legs at
`p = 1/n`, `sum(p(1-p)) -> 1 - 1/n`. So an n-leg set needs ~7% gross edge on
Kalshi and ~4% on Polymarket politics - but *any* positive edge on geopolitics.

**A live 8-leg Kalshi set showed `sum(YES) = $0.98`** - an apparent 2% risk-free
profit. After fees it was a **6% loss**. That case is a permanent regression test.

**Kalshi order books are bids-only for both sides.** `yes_ask = 1 - best_no_bid`.
Reading the `yes_dollars` ladder as asks - the obvious mistake - produces prices
wrong by the entire width of the market.

**Roughly half the top-25 Polymarket wallets are market makers.** Volume:P&L
ratios of 35:1 to 155:1. The rank-1 wallet was running Brazilian football and ATP
tennis at high frequency with one position at -99.9%. Copying that is
volunteering to be their exit liquidity.

**`takerBaseFee` is not a rate.** It reads 1000 across every category. The real
decimal rate is `feeSchedule.rate`. Passing the former into the fee formula
inflated costs ~1000x and silently rejected every candidate in the universe while
the scanner appeared healthy. There is now a bounds check that raises instead.

---

## Observed funnel (single live scan, 2026-07-21)

```
              9,707  events ingested (both venues)
              4,007  mutually exclusive
                218  legs aligned on resolution date
                191  fully priced
                 57  gross-positive
                 20  survive the fee burden
                  0  survive the order-book walk
```

Zero is the correct answer that day, and it is the point. Every apparent arb was
either a fee mirage or a top-of-book phantom. A system that finds a trade every
day is not finding edge, it is lowering its standards.

---

## Layout

```
config.yaml                 one number drives everything
src/edge_engine/
  ingest/    kalshi.py polymarket.py models.py http.py
  store/     sqlite_store.py          <- the deploy seam
  strategies/ combinatorial.py wallet_signal.py base.py
  sizing/    fees.py kelly.py bankroll.py
  journal/   calibration.py
  alert/     telegram.py
  scan.py    orchestrator + CLI
probe/       live API diagnostics (funnel, book depth, fee fields)
tests/       74 tests
docs/superpowers/specs/     design spec
```

Storage sits behind an interface so promoting to Postgres/cloud later means
writing one class, not editing strategies.

---

## Deliberately not built yet

| | |
|---|---|
| Weather model (NOAA vs Kalshi implied) | needs journal data to validate |
| Favorite-longshot correction | needs collected snapshots to fit |
| Cross-venue matching + execution | gated at $15k; logging only until then |
| Cloud promotion | prove the edge locally first |

Forecasting strategies are gated behind the journal producing enough resolved
signals to validate them. Shipping a model before you can measure its calibration
is how these systems lose money confidently.
