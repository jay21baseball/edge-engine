"""Scanner orchestrator and CLI.

    python -m edge_engine.scan scan       one pass, print findings
    python -m edge_engine.scan wallets    rebuild the qualified-wallet list
    python -m edge_engine.scan watch      continuous loop
    python -m edge_engine.scan status     bankroll, discipline, calibration
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from .alert.rules import Alerter, AlertPolicy, format_alert
from .alert.telegram import TelegramNotifier, build_briefing
from .ingest.kalshi import KalshiClient
from .ingest.models import Event, Market, OrderBook, Venue
from .ingest.odds_api import DEFAULT_SPORTS, OddsApiClient
from .ingest.polymarket import PolymarketClient
from .journal.calibration import build_report
from .sizing.bankroll import BankrollConfig, DisciplineState
from .sizing.kelly import size_position
from .store.sqlite_store import SqliteStore
from .strategies.base import Signal
from .strategies.combinatorial import CombinatorialArb
from .strategies.sportsbook import SportsbookDivergence
from .strategies.wallet_signal import WalletAttentionQueue, score_wallet

log = logging.getLogger("edge_engine")

# A snapshot older than this cannot generate an alert. Stale data producing a
# confident-looking arb is the fastest way to lose money in this whole system.
MAX_SNAPSHOT_AGE_SECONDS = 300


@dataclass
class ScanContext:
    events: list[Event]
    book_fetcher: Callable[[Market], Optional[OrderBook]]
    odds_events: list = field(default_factory=list)
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_stale(self) -> bool:
        age = (datetime.now(timezone.utc) - self.fetched_at).total_seconds()
        return age > MAX_SNAPSHOT_AGE_SECONDS


def _make_store(config: dict[str, Any]):
    """Pick storage. This is the seam: nothing downstream knows the difference.

    Secrets come from the environment first so the GitHub Actions workflow can
    inject them without any of them touching config.yaml or the repo.
    """
    dsn = os.environ.get("DATABASE_URL") or config.get("database_url")
    backend = (os.environ.get("EDGE_STORE") or config.get("store") or "").lower()
    if dsn and backend != "sqlite":
        from .store.postgres_store import PostgresStore
        log.info("using Postgres store")
        return PostgresStore(dsn)
    return SqliteStore(config["db_path"])


def load_config(path: str = "config.yaml") -> dict[str, Any]:
    defaults = {
        "bankroll": 2500.0, "kelly_fraction": 0.25,
        "max_single_position_pct": 5.0, "max_concurrent_exposure_pct": 40.0,
        "min_edge_threshold_pct": 4.0, "max_trades_per_day": 3,
        "drawdown_circuit_breaker_pct": 15.0,
        "telegram_bot_token": None, "telegram_chat_id": None,
        "scan_interval_seconds": 300, "target_contracts": 100,
        "db_path": "data/edge.db",
        "wallet_refresh_hours": 6, "wallet_score_ttl_hours": 24,
        "odds_api_key": None, "odds_sports": None,
        "store": None, "database_url": None,
    }
    file = Path(path)
    if file.exists():
        try:
            import yaml
            loaded = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
            defaults.update({k: v for k, v in loaded.items() if v is not None})
        except Exception as e:
            log.warning("could not read %s (%s), using defaults", path, e)

    # Environment wins over the file. Keeps secrets out of the repo entirely and
    # lets the GitHub Actions workflow inject them as encrypted secrets.
    for env_key, config_key, cast in (
        ("TELEGRAM_BOT_TOKEN", "telegram_bot_token", str),
        ("TELEGRAM_CHAT_ID", "telegram_chat_id", str),
        ("ODDS_API_KEY", "odds_api_key", str),
        ("EDGE_BANKROLL", "bankroll", float),
    ):
        raw = os.environ.get(env_key)
        if raw:
            try:
                defaults[config_key] = cast(raw)
            except ValueError:
                log.warning("ignoring malformed %s", env_key)
    return defaults


class Engine:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.kalshi = KalshiClient()
        self.poly = PolymarketClient()
        self.store = _make_store(config)
        self.bankroll = BankrollConfig(
            bankroll=float(config["bankroll"]),
            kelly_fraction=float(config["kelly_fraction"]),
            max_single_position_pct=float(config["max_single_position_pct"]),
            max_concurrent_exposure_pct=float(config["max_concurrent_exposure_pct"]),
            min_edge_threshold_pct=float(config["min_edge_threshold_pct"]),
            max_trades_per_day=int(config["max_trades_per_day"]),
            drawdown_circuit_breaker_pct=float(config["drawdown_circuit_breaker_pct"]),
        )
        self.state = DisciplineState(config=self.bankroll)
        self.notifier = TelegramNotifier(
            config.get("telegram_bot_token"), config.get("telegram_chat_id")
        )
        self.arb = CombinatorialArb(target_contracts=float(config["target_contracts"]))
        self.attention = WalletAttentionQueue()
        self.odds = OddsApiClient(config.get("odds_api_key"))
        self.sportsbook = SportsbookDivergence()
        self.alerter = Alerter(self.store, AlertPolicy(
            max_per_day=int(config.get("max_alerts_per_day", 5)),
            respect_quiet_hours=bool(config.get("respect_quiet_hours", True)),
            quiet_start_hour=int(config.get("quiet_start_hour", 23)),
            quiet_end_hour=int(config.get("quiet_end_hour", 7)),
        ))
        self._book_cache: dict[str, OrderBook] = {}
        self._odds_cache: list = []
        self._odds_fetched_at: float = -1e9
        self._last_scan_at: Optional[float] = None

    # ------------------------------------------------------------- ingestion

    def fetch_events(self) -> list[Event]:
        events: list[Event] = []
        for name, fn in (("kalshi", self._kalshi_events),
                         ("polymarket", self._poly_events)):
            try:
                found = fn()
                log.info("%s: %d events, %d mutually-exclusive", name, len(found),
                         sum(1 for e in found if e.is_mece))
                events.extend(found)
            except Exception as e:
                # Never let one venue's outage look like "no opportunities today".
                log.error("%s ingest failed: %s", name, e)
        return events

    def _kalshi_events(self) -> list[Event]:
        return self.kalshi.events(status="open", with_markets=True)

    def _poly_events(self) -> list[Event]:
        return self.poly.events(limit=600, closed=False)

    def _fetch_book(self, market: Market) -> Optional[OrderBook]:
        cache_key = market.key
        if cache_key in self._book_cache:
            return self._book_cache[cache_key]
        try:
            if market.venue is Venue.KALSHI:
                book = self.kalshi.orderbook(market.market_id)
            else:
                # YES and NO are separate ERC-1155 tokens with independent order
                # books. Deriving the NO ask from the YES token's bids prices a
                # SELL of YES, not a BUY of NO - a different and usually worse
                # trade. Merge both real books so NO-side arbs are priced against
                # liquidity that actually exists.
                tokens = self.poly.token_ids(market.raw)
                if not tokens:
                    return None
                book = self.poly.orderbook(tokens[0])
                if len(tokens) > 1:
                    try:
                        no_book = self.poly.orderbook(tokens[1])
                        book.no_asks = no_book.yes_asks
                        book.no_bids = no_book.yes_bids
                    except Exception as e:
                        log.debug("NO-token book failed for %s: %s", market.key, e)
            self._book_cache[cache_key] = book
            return book
        except Exception as e:
            log.debug("book fetch failed for %s: %s", market.key, e)
            return None

    # ------------------------------------------------------------------ scan

    def scan_once(self) -> list[Signal]:
        self._book_cache.clear()
        self._last_scan_at = time.monotonic()
        events = self.fetch_events()
        if not events:
            log.warning("no events ingested - skipping scan rather than "
                        "reporting a clean slate")
            return []

        markets = [m for e in events for m in e.markets]
        self.store.upsert_events(events)
        self.store.upsert_markets(markets)
        log.info("stored %d markets across %d events", len(markets), len(events))

        odds_events = self._odds_events()

        context = ScanContext(events=events, book_fetcher=self._fetch_book,
                              odds_events=odds_events)
        signals = self.arb.scan(context)
        signals.extend(self._wallet_signals(events))
        signals.extend(self.sportsbook.scan(context))

        if context.is_stale:
            log.warning("data went stale during scan - suppressing all alerts")
            return []

        kept = self._apply_discipline(sorted(signals, key=lambda s: -s.score))
        self._last_events = events
        # Shared across processes so the bot can reuse a fresh scan instead of
        # running its own and timing out the Telegram reply.
        self.store.set_state("_system", "last_scan_at",
                             datetime.now(timezone.utc).isoformat())
        return kept

    def _odds_due(self, every_minutes: float) -> bool:
        """Wall-clock gate, so the throttle survives ephemeral cloud runs.

        In `watch` the process is long-lived and an in-memory timer works. Under
        GitHub Actions every scan is a FRESH process, so an in-memory timer
        resets each pass and would fetch odds every single run - 5,760/month
        against a 500 free-tier allowance, killing the key in under three days.

        Anchoring to UTC wall-clock instead makes the decision identical whether
        the process has been alive for a week or four seconds.
        """
        if every_minutes <= 0:
            return True
        now = datetime.now(timezone.utc)
        minute_of_day = now.hour * 60 + now.minute
        window = float(self.config.get("scan_interval_seconds", 300)) / 60.0
        return (minute_of_day % every_minutes) < max(window, 1.0)

    def _odds_events(self) -> list:
        """Sharp lines, throttled to fit the odds-API quota.

        Odds cost one request per sport per fetch, and The Odds API free tier is
        500 requests/MONTH. At 15-minute scans, fetching every pass would burn
        5,760 - eleven times the allowance - and the key would die in under three
        days. So odds refresh on their own timer and are reused in between.

        The honest tradeoff: the timing-lag edge decays in minutes, so a
        3-hourly line catches slow divergence but NOT the injury-news lag that
        makes this strategy sharp. Working that properly needs the paid tier.
        `odds_refresh_minutes` is the dial; the default is sized for the free
        tier, not for maximum edge.
        """
        if not self.odds.enabled:
            return []
        every = float(self.config.get("odds_refresh_minutes", 180))

        if not self._odds_due(every):
            log.debug("odds not due this pass (refresh every %.0f min)", every)
            return self._odds_cache

        elapsed = (time.monotonic() - self._odds_fetched_at) / 60.0
        if self._odds_cache and elapsed < every:
            log.debug("reusing odds from %.0f min ago", elapsed)
            return self._odds_cache
        try:
            sports = tuple(self.config.get("odds_sports") or DEFAULT_SPORTS)
            self._odds_cache = self.odds.fetch_all(sports)
            self._odds_fetched_at = time.monotonic()
            log.info("odds refreshed: %d sports, ~%.0f requests/month at this rate",
                     len(sports), len(sports) * (43200.0 / max(every, 1.0)))
        except Exception as e:
            log.error("odds fetch failed, reusing cache: %s", e)
        return self._odds_cache

    def _wallet_signals(self, events: list[Event]) -> list[Signal]:
        """Attention queue from qualified wallets.

        Wallet scoring is expensive (leaderboard sweep plus per-wallet enrichment)
        so it refreshes on its own slower cadence, while their live positions are
        re-read each scan. If scores are stale or nobody qualifies, this returns
        nothing rather than falling back to unscreened leaderboard names - an
        unscreened wallet is exactly the market maker the whole screen exists to
        keep out.
        """
        qualified = self.store.qualified_wallets(
            max_age_hours=float(self.config.get("wallet_score_ttl_hours", 24))
        )
        if not qualified:
            age = self.store.wallet_score_age_hours()
            log.info("no qualified wallets cached%s - run 'wallets' to refresh",
                     f" (scores {age:.1f}h old)" if age else "")
            return []

        prices: dict[str, float] = {}
        horizons: dict[str, float] = {}
        for event in events:
            if event.venue is not Venue.POLYMARKET:
                continue
            for market in event.markets:
                condition_id = str(market.raw.get("conditionId") or "")
                if condition_id and market.yes_ask:
                    prices[condition_id] = market.yes_ask
                    horizons[condition_id] = market.days_to_resolution

        scores, positions = {}, {}
        for address in qualified[:60]:
            try:
                held = self.poly.positions(address)
            except Exception as e:
                log.debug("positions failed for %s: %s", address, e)
                continue
            cached = self._cached_score(address)
            if cached:
                scores[address] = cached
                positions[address] = held

        if not scores:
            return []
        found = self.attention.build(scores, positions, prices,
                                     days_to_resolution=horizons)
        log.info("wallet attention: %d qualified wallets -> %d signals",
                 len(scores), len(found))
        return found

    def _cached_score(self, address: str):
        """Rehydrate a stored WalletScore without re-running the full enrichment."""
        from .strategies.wallet_signal import WalletScore
        row = self.store.latest_wallet_score(address)
        if not row:
            return None
        return WalletScore(
            address=address, username=row["username"] or "",
            category=row["category"] or "OVERALL",
            n_resolved=row["n_resolved"] or 0,
            entry_adjusted_edge=row["entry_adj_edge"] or 0.0,
            t_stat=row["t_stat"] or 0.0,
            brier=row["brier"] if row["brier"] is not None else 1.0,
            pnl_herfindahl=row["herfindahl"] if row["herfindahl"] is not None else 1.0,
            volume_to_pnl=(row["volume_to_pnl"]
                           if row["volume_to_pnl"] is not None else float("inf")),
            recency_weight=1.0, qualified=True,
        )

    def _apply_discipline(self, signals: list[Signal]) -> list[Signal]:
        """Size every signal and drop the ones the rules refuse."""
        kept: list[Signal] = []
        # Near-misses are recorded so /whynot can show what was rejected and
        # why. Seeing the strongest thing you turned down is the only way to
        # tell a well-calibrated threshold from one that is quietly bleeding
        # opportunity.
        rejected: list[dict] = []

        def note(signal: Signal, reason: str) -> None:
            rejected.append({
                "title": signal.title[:70], "strategy": signal.strategy,
                "edge": signal.edge, "price": signal.entry_price,
                "days": signal.days_to_resolution, "reason": reason,
            })

        for signal in signals:
            # Advisory signals are not trades, so the trade caps do not apply to
            # them - only the strategy gate and the edge floor.
            ok, why = self.state.can_trade(
                signal.strategy,
                0.0 if signal.advisory else signal.edge,
                deterministic=signal.deterministic,
            )
            if not ok and not signal.advisory:
                log.info("suppressed '%s': %s", signal.title[:40], why)
                note(signal, why)
                continue
            if self.store.recent_signal_exists(signal.strategy, signal.market_id):
                log.debug("duplicate within window: %s", signal.title[:40])
                continue

            if signal.advisory or self.bankroll.is_paper_mode:
                # Advisory signals carry no forecast to size. Paper mode is the
                # other case: below the practical minimum the analysis is still
                # worth reading, but printing a stake figure while telling the
                # operator not to use it works against the whole point of
                # proving the edge before funding it.
                signal.stake = None
                signal.contracts = None
            else:
                sizing = size_position(
                    prob=signal.est_probability, price=signal.entry_price,
                    bankroll=self.bankroll.bankroll,
                    kelly_multiplier=self.bankroll.kelly_fraction,
                    max_single_position_pct=self.bankroll.max_single_position_pct,
                    available_capital=self.state.available_capital,
                    min_order_size=5.0 if signal.venue is Venue.POLYMARKET else 1.0,
                )
                if signal.deterministic and not sizing.is_actionable:
                    # A locked arb has no probabilistic edge for Kelly to size,
                    # so fall back to the exposure cap rather than dropping it.
                    stake = min(self.state.available_capital,
                                self.bankroll.unit_size())
                    signal.stake = round(stake, 2)
                    signal.contracts = round(stake / max(signal.entry_price, 0.01), 0)
                elif sizing.is_actionable:
                    signal.stake = sizing.stake
                    signal.contracts = sizing.contracts
                else:
                    # Never drop silently. A signal vanishing without explanation
                    # is indistinguishable from the scanner being broken.
                    log.info("dropped '%s': not sizeable (%s)",
                             signal.title[:40], sizing.capped_by)
                    note(signal, f"not sizeable ({sizing.capped_by})")
                    continue

            signal_id = self.store.save_signal(signal)
            self.store.mark_alerted(signal_id)
            kept.append(signal)

        rejected.sort(key=lambda r: -r["edge"])
        self.store.set_state("_system", "last_rejections", rejected[:10])
        self._last_events = getattr(self, "_last_events", None)
        return kept

    # --------------------------------------------------------------- wallets

    def rebuild_wallets(self, max_wallets: int = 150) -> dict[str, Any]:
        log.info("sweeping leaderboard across all windows and categories...")
        discovered = self.poly.discover_wallets()
        log.info("discovered %d unique wallets", len(discovered))

        scores, positions_by_wallet = {}, {}
        # Category leaderboards return rows with vol=0, which sorted to the FRONT
        # of an ascending volume:P&L ranking and consumed the entire enrichment
        # budget on wallets with no usable data. Require real volume and profit
        # before a row is worth spending API calls on.
        usable = [e for e in discovered.values() if e.volume > 0 and e.pnl > 0]
        log.info("%d of %d discovered rows have usable volume/pnl",
                 len(usable), len(discovered))
        # Cheapest screen first: volume:P&L needs no extra calls and eliminates
        # market makers before we pay to enrich them.
        ranked = sorted(usable, key=lambda e: e.volume_to_pnl)
        for entry in ranked[:max_wallets]:
            try:
                # Resolved history drives the skill screen; live holdings drive
                # the attention signal. They come from different endpoints.
                history = self.poly.closed_positions(entry.address)
                open_positions = self.poly.positions(entry.address)
                activity = self.poly.activity(entry.address)
            except Exception as e:
                log.debug("enrich failed for %s: %s", entry.address, e)
                continue
            score = score_wallet(entry.address, entry, history, activity)
            scores[entry.address] = score
            positions_by_wallet[entry.address] = open_positions

        self.store.save_wallet_scores(scores.values())
        qualified = {a: s for a, s in scores.items() if s.qualified}
        mm = sum(1 for s in scores.values() if s.is_market_maker)
        log.info("scored %d wallets: %d qualified, %d excluded as market makers",
                 len(scores), len(qualified), mm)
        return {"scores": scores, "positions": positions_by_wallet,
                "qualified": qualified, "market_makers": mm}

    def push_alerts(self, signals: list[Signal], events=None) -> int:
        """Push anything good enough to interrupt for. Returns how many fired.

        Separate from the daily briefing on purpose: the briefing is pull (you
        ask), this is push (something appeared that will not keep). The rules in
        alert.rules are bars to clear, not filters to pass - an alerter that
        fires on everything trains you to ignore it.
        """
        chat_id = str(self.config.get("telegram_chat_id") or "")
        if not chat_id:
            return 0

        decisions = self.alerter.select(signals, chat_id)
        for decision in decisions:
            self.notifier.send(format_alert(decision))
        if decisions:
            # Record only what was actually sent, in one pass - re-running
            # select() here would return a different set now that the daily
            # count has moved.
            self.alerter.record_sent(chat_id, decisions)
            log.info("pushed %d alert(s)", len(decisions))
        sent = len(decisions)

        # Watchlist triggers are a separate channel: the operator asked to be
        # told about a price, so no edge assessment gates them.
        if events:
            from .bot.commands import check_watchlist
            for text in check_watchlist(self, chat_id, events):
                self.notifier.send(text)
                sent += 1
        return sent

    # ------------------------------------------------------------ bot support

    def has_bankroll(self, chat_id: str) -> bool:
        return self.store.get_state(chat_id, "bankroll") is not None

    def set_bankroll(self, chat_id: str, amount: float) -> None:
        """Resize everything from one number, and persist it per chat."""
        self.store.set_state(chat_id, "bankroll", amount)
        self.bankroll.bankroll = amount
        self.state.config = self.bankroll
        if self.state.current_bankroll <= 0:
            self.state.current_bankroll = amount
        if self.state.week_start_bankroll <= 0:
            self.state.week_start_bankroll = amount

    def load_bankroll(self, chat_id: str) -> None:
        stored = self.store.get_state(chat_id, "bankroll")
        if stored:
            self.set_bankroll(chat_id, float(stored))

    def minutes_since_scan(self) -> Optional[float]:
        """Age of the most recent scan, ACROSS processes.

        The scanner and the bot are separate processes. An in-memory timestamp
        is always None in the bot, so every command triggered a fresh 40-second
        scan and Telegram timed out before the reply arrived. The scan time is
        therefore recorded in the store, which both processes share.
        """
        stamp = self.store.get_state("_system", "last_scan_at")
        if not stamp:
            return None
        try:
            scanned = datetime.fromisoformat(str(stamp))
        except ValueError:
            return None
        if scanned.tzinfo is None:
            scanned = scanned.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - scanned).total_seconds() / 60.0

    def remember_briefing(self, chat_id: str, rows: list[dict], window) -> None:
        """Map the numbers shown in a briefing back to signal ids.

        Without this, '/took 1' has nothing to point at. The order must match
        exactly what build_briefing rendered, so the same filter and sort are
        applied here.
        """
        eligible = [
            r for r in rows
            if (r.get("days_to_resolution") or 0) <= window.max_days
        ]
        eligible.sort(key=lambda r: -(r.get("score") or 0))
        ids = [r["id"] for r in eligible[: window.max_plays] if r.get("id")]
        self.store.set_state(chat_id, "briefing_ids", ids)

    # --------------------------------------------------------------- reports

    def calibration_verdict(self) -> str:
        return build_report(self.store.resolved_predictions()).verdict()

    def briefing(self, signals: list[Signal]) -> str:
        return build_briefing(signals, self.state, self.calibration_verdict())


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="edge-engine")
    parser.add_argument("command",
                        choices=["scan", "watch", "wallets", "status", "sports",
                                 "signals", "bot"])
    parser.add_argument("--all", action="store_true",
                        help="sports: include out-of-season leagues")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    )
    engine = Engine(load_config(args.config))

    if args.command == "status":
        print(engine.state.status_line())
        print(f"unit size: ${engine.bankroll.unit_size():,.2f}")
        print(f"edge floor: {engine.bankroll.effective_min_edge * 100:.2f}%")
        print(f"\n{engine.calibration_verdict()}")
        print("\nstrategy gates:")
        for name, gate in engine.bankroll.strategy_gates.items():
            mark = "ON " if engine.bankroll.strategy_enabled(name) else "OFF"
            print(f"  [{mark}] {name:<22} (needs ${gate:,.0f})")
        print(f"\nstored rows: {engine.store.counts()}")
        return 0

    if args.command == "bot":
        from .bot.listener import TelegramBot
        token = engine.config.get("telegram_bot_token")
        chat_id = engine.config.get("telegram_chat_id")
        if not token:
            print("No TELEGRAM_BOT_TOKEN set. Run setup_telegram first.")
            return 1
        if chat_id:
            engine.load_bankroll(str(chat_id))
        TelegramBot(engine, token, allowed_chat_id=chat_id).run()
        return 0

    if args.command == "signals":
        rows = engine.store.recent_signals(limit=25)
        if not rows:
            print("No signals recorded yet. Run a scan first.")
            return 0
        print(f"{len(rows)} most recent signals (newest first):\n")
        for r in rows:
            rationale = json.loads(r["rationale"] or "{}")
            kind = "ARB " if r["deterministic"] else "WATCH"
            print(f"[{kind}] {r['title'][:56]}")
            print(f"        {r['venue']} {r['side']} @ {r['entry_price']:.3f}"
                  f"   edge {r['edge'] * 100:+.2f}%"
                  f"   {r['days_to_resolution']:.1f}d"
                  f"   stake ${r['stake'] or 0:,.2f}")
            if r["strategy"] == "wallet_attention":
                print(f"        {rationale.get('agreeing_wallets')} wallets in at "
                      f"{rationale.get('their_avg_entry')}, now "
                      f"{rationale.get('current_price')} - "
                      f"{rationale.get('move_already_captured_pct')}% of the move "
                      f"already gone")
            elif r["strategy"] == "sportsbook_divergence":
                print(f"        sharp says {rationale.get('devigged_probability')}, "
                      f"Polymarket asks {rationale.get('polymarket_ask')} "
                      f"({rationale.get('sharp_source')})")
            print()
        return 0

    if args.command == "sports":
        if not engine.odds.enabled:
            print("No odds_api_key set. Free key: https://the-odds-api.com")
            return 1
        rows = engine.odds.list_sports(all_sports=args.all)
        active = engine.config.get("odds_sports") or DEFAULT_SPORTS
        groups: dict[str, list[dict]] = {}
        for r in rows:
            groups.setdefault(r.get("group", "Other"), []).append(r)
        for group in sorted(groups):
            print(f"\n{group}")
            for r in sorted(groups[group], key=lambda x: x.get("key", "")):
                mark = " *" if r.get("key") in active else "  "
                print(f" {mark} {r.get('key', ''):<44} {r.get('title', '')}")
        every = float(engine.config.get("odds_refresh_minutes", 180))
        monthly = len(active) * (43200.0 / max(every, 1.0))
        print(f"\n* = currently scanned. Edit `odds_sports` in config.yaml.")
        print(f"\nQuota: 1 request per sport per refresh. You have "
              f"{len(active)} sport(s) refreshing every {every:.0f} min")
        print(f"  -> ~{monthly:.0f} requests/month "
              f"({'OVER' if monthly > 500 else 'within'} the 500 free tier)")
        print("\nTo add a sport: paste its key into `odds_sports`. To keep the same")
        print("quota, either raise `odds_refresh_minutes` or drop another sport.")
        return 0

    if args.command == "wallets":
        result = engine.rebuild_wallets()
        print(f"\nqualified wallets: {len(result['qualified'])} "
              f"of {len(result['scores'])} scored "
              f"({result['market_makers']} excluded as market makers)")
        for address, score in sorted(result["qualified"].items(),
                                     key=lambda kv: -kv[1].composite)[:15]:
            print(f"  {score.username or address[:12]:<20} "
                  f"edge {score.entry_adjusted_edge:+.3f}  "
                  f"t={score.t_stat:.1f}  n={score.n_resolved}  "
                  f"vol:pnl={score.volume_to_pnl:.1f}")
        if not result["qualified"]:
            print("  (none passed - that is a real result, not a bug)")
        return 0

    if args.command == "scan":
        signals = engine.scan_once()
        engine.push_alerts(signals, getattr(engine, "_last_events", None))
        engine.notifier.send(engine.briefing(signals))
        return 0

    if args.command == "watch":
        interval = int(engine.config["scan_interval_seconds"])
        wallet_every = float(engine.config.get("wallet_refresh_hours", 6))
        log.info("watching every %ds, wallet rescore every %.1fh. ctrl-c to stop.",
                 interval, wallet_every)
        last_wallet_refresh = 0.0
        while True:
            try:
                # Rescore wallets on a slow cadence - the leaderboard sweep plus
                # per-wallet enrichment is far too expensive to run every scan.
                age = engine.store.wallet_score_age_hours()
                due = age is None or age >= wallet_every
                if due and (time.monotonic() - last_wallet_refresh) > 1800:
                    log.info("refreshing wallet scores...")
                    engine.rebuild_wallets()
                    last_wallet_refresh = time.monotonic()

                signals = engine.scan_once()
                # Push first: anything urgent should not wait behind a digest.
                pushed = engine.push_alerts(
                    signals, getattr(engine, "_last_events", None)
                )
                if signals and not pushed:
                    log.info("%d signal(s) found, none cleared the alert bar",
                             len(signals))
                elif not signals:
                    log.info("nothing cleared threshold")
            except KeyboardInterrupt:
                log.info("stopped")
                return 0
            except Exception as e:
                log.error("scan failed, continuing: %s", e, exc_info=True)
            time.sleep(interval)

    return 0


if __name__ == "__main__":
    sys.exit(main())
