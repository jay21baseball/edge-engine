"""SQLite storage. The interface here IS the deploy seam.

Everything downstream depends only on these method signatures, so promoting to
Postgres later means writing one new class, not editing strategies.

Snapshotting every market on every scan is deliberate and is the highest-return
decision in the project: within weeks it becomes a historical price series for
both venues that cannot be bought, and it is what makes backtesting and
calibration possible at all.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from ..ingest.models import Market, Venue
from ..strategies.base import Signal

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    key TEXT PRIMARY KEY, venue TEXT, market_id TEXT, event_id TEXT,
    title TEXT, category TEXT, close_ts TEXT, status TEXT,
    fee_rate REAL, first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT, ts TEXT, yes_bid REAL, yes_ask REAL, no_bid REAL, no_ask REAL,
    volume REAL, liquidity REAL
);
CREATE INDEX IF NOT EXISTS ix_snap_key_ts ON market_snapshots(key, ts);

CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY, venue TEXT, event_id TEXT, title TEXT,
    category TEXT, mutually_exclusive INTEGER, neg_risk INTEGER, last_seen TEXT
);

CREATE TABLE IF NOT EXISTS wallet_scores (
    address TEXT, ts TEXT, username TEXT, category TEXT, n_resolved INTEGER,
    entry_adj_edge REAL, t_stat REAL, brier REAL, herfindahl REAL,
    volume_to_pnl REAL, is_mm INTEGER, qualified INTEGER, disqualified_for TEXT,
    PRIMARY KEY (address, ts)
);
CREATE TABLE IF NOT EXISTS wallet_positions (
    address TEXT, market_id TEXT, side TEXT, size REAL, avg_price REAL,
    current_price REAL, observed_ts TEXT,
    PRIMARY KEY (address, market_id, side, observed_ts)
);

CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, strategy TEXT, venue TEXT, market_id TEXT, title TEXT, side TEXT,
    entry_price REAL, est_probability REAL, edge REAL, confidence REAL,
    days_to_resolution REAL, score REAL, deterministic INTEGER,
    category TEXT, stake REAL, contracts REAL,
    legs TEXT, rationale TEXT, counter_case TEXT, alerted INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS journal (
    signal_id INTEGER PRIMARY KEY,
    alerted_ts TEXT, taken INTEGER DEFAULT 0, actual_entry REAL, stake REAL,
    outcome REAL, pnl REAL, resolved_ts TEXT, notes TEXT,
    FOREIGN KEY (signal_id) REFERENCES signals(id)
);

CREATE TABLE IF NOT EXISTS bot_state (
    chat_id TEXT, key TEXT, value TEXT, updated TEXT,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS live_divergence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, league TEXT, game TEXT, team TEXT, detail TEXT,
    espn_prob REAL, poly_price REAL, gap REAL, net_gap REAL,
    espn_delta REAL, poly_delta REAL, market_id TEXT
);
CREATE INDEX IF NOT EXISTS ix_live_ts ON live_divergence(ts);
CREATE INDEX IF NOT EXISTS ix_live_game ON live_divergence(game, ts);

CREATE TABLE IF NOT EXISTS cross_venue_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT, kalshi_id TEXT, poly_id TEXT, title TEXT,
    gross_spread REAL, net_after_fees REAL, depth_available REAL, confirmed INTEGER
);
"""


def _iso(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


class SqliteStore:
    def __init__(self, path: str | Path = "data/edge.db"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- ingestion

    def _changed_only(self, conn, rows: list[Market]) -> list[Market]:
        """Drop markets whose quote is unchanged since the last snapshot.

        Snapshotting all ~88k markets every scan is unbounded: at 15-minute
        scans that is ~8M rows/day and the database reached 2.2 GB in a single
        day of testing — past the entire Supabase free tier. The overwhelming
        majority of markets do not move between scans, so storing an identical
        row is pure cost with no analytical value. Only actual price changes
        are recorded, which is also what a price series should contain.
        """
        latest = {
            r[0]: (r[1], r[2], r[3], r[4])
            for r in conn.execute(
                """SELECT key, yes_bid, yes_ask, no_bid, no_ask
                   FROM market_snapshots
                   WHERE id IN (SELECT MAX(id) FROM market_snapshots GROUP BY key)"""
            ).fetchall()
        }
        changed = []
        for m in rows:
            previous = latest.get(m.key)
            current = (m.yes_bid, m.yes_ask, m.no_bid, m.no_ask)
            if previous is None or previous != current:
                changed.append(m)
        return changed

    # ------------------------------------------------------------------ live

    def last_live_prices(self, market_ids: list[str]) -> dict[str, tuple]:
        """Most recent (espn_prob, poly_price) per market, for delta/lead-lag."""
        if not market_ids:
            return {}
        marks = ",".join("?" * len(market_ids))
        with self.connect() as conn:
            rows = conn.execute(
                f"""SELECT market_id, espn_prob, poly_price FROM live_divergence
                    WHERE id IN (SELECT MAX(id) FROM live_divergence
                                 WHERE market_id IN ({marks}) GROUP BY market_id)""",
                market_ids,
            ).fetchall()
        return {r[0]: (r[1], r[2]) for r in rows}

    def log_live(self, records: list[dict]) -> int:
        """Append divergence observations. Records carry precomputed deltas."""
        if not records:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO live_divergence
                       (ts, league, game, team, detail, espn_prob, poly_price,
                        gap, net_gap, espn_delta, poly_delta, market_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(now, r["league"], r["game"], r["team"], r["detail"],
                  r["espn_prob"], r["poly_price"], r["gap"], r["net_gap"],
                  r["espn_delta"], r["poly_delta"], r["market_id"])
                 for r in records],
            )
        return len(records)

    def live_summary(self, since_hours: float = 168.0) -> dict:
        """Aggregate the recorded divergence: is there a lead, and gaps that last?"""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT COUNT(*), COUNT(DISTINCT game),
                          AVG(ABS(gap)), MAX(ABS(gap)),
                          AVG(CASE WHEN net_gap > 0 THEN 1.0 ELSE 0.0 END)
                   FROM live_divergence WHERE ts > datetime('now', ?)""",
                (f"-{since_hours} hours",),
            ).fetchone()
            # Lead attribution: on observations where the gap widened, which
            # side moved? If ESPN moved and Polymarket did not, ESPN led.
            lead = conn.execute(
                """SELECT
                     SUM(CASE WHEN ABS(espn_delta) > ABS(poly_delta)
                              THEN 1 ELSE 0 END) AS espn_led,
                     SUM(CASE WHEN ABS(poly_delta) > ABS(espn_delta)
                              THEN 1 ELSE 0 END) AS poly_led,
                     COUNT(*) AS moves
                   FROM live_divergence
                   WHERE ts > datetime('now', ?)
                     AND (ABS(espn_delta) > 0.001 OR ABS(poly_delta) > 0.001)""",
                (f"-{since_hours} hours",),
            ).fetchone()
        return {
            "observations": row[0] or 0,
            "games": row[1] or 0,
            "avg_gap": row[2] or 0.0,
            "max_gap": row[3] or 0.0,
            "pct_net_positive": row[4] or 0.0,
            "espn_led": lead[0] or 0,
            "poly_led": lead[1] or 0,
            "moves": lead[2] or 0,
        }

    def biggest_live_gaps(self, limit: int = 8, since_hours: float = 1.0
                          ) -> list[dict]:
        """Latest observation per market, ranked by gap. One row per game side,
        not one per poll - otherwise the same market repeats down the list."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT game, team, detail, espn_prob, poly_price, gap, net_gap, ts
                   FROM live_divergence
                   WHERE id IN (SELECT MAX(id) FROM live_divergence
                                WHERE ts > datetime('now', ?) GROUP BY market_id)
                   ORDER BY ABS(net_gap) DESC LIMIT ?""",
                (f"-{since_hours} hours", limit),
            ).fetchall()
        cols = ("game", "team", "detail", "espn_prob", "poly_price",
                "gap", "net_gap", "ts")
        return [dict(zip(cols, r)) for r in rows]

    def prune_live(self, keep_days: float = 30.0) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM live_divergence WHERE ts < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
        return max(cur.rowcount, 0)

    def prune_snapshots(self, keep_days: float = 90.0) -> int:
        """Drop snapshots older than the retention window."""
        with self.connect() as conn:
            cur = conn.execute(
                "DELETE FROM market_snapshots WHERE ts < datetime('now', ?)",
                (f"-{keep_days} days",),
            )
            deleted = cur.rowcount
        return max(deleted, 0)

    def upsert_markets(self, markets: Iterable[Market]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = list(markets)
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO markets (key, venue, market_id, event_id, title,
                       category, close_ts, status, fee_rate, first_seen, last_seen)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       title=excluded.title, status=excluded.status,
                       close_ts=excluded.close_ts, last_seen=excluded.last_seen""",
                [(m.key, m.venue.value, m.market_id, m.event_id, m.title, m.category,
                  _iso(m.close_ts), m.status, m.fee_rate, now, now) for m in rows],
            )
            changed = self._changed_only(conn, rows)
            conn.executemany(
                """INSERT INTO market_snapshots
                       (key, ts, yes_bid, yes_ask, no_bid, no_ask, volume, liquidity)
                   VALUES (?,?,?,?,?,?,?,?)""",
                [(m.key, now, m.yes_bid, m.yes_ask, m.no_bid, m.no_ask,
                  m.volume, m.liquidity) for m in changed],
            )
        return len(rows)

    def upsert_events(self, events: Iterable[Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = list(events)
        with self.connect() as conn:
            conn.executemany(
                """INSERT INTO events (key, venue, event_id, title, category,
                       mutually_exclusive, neg_risk, last_seen)
                   VALUES (?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET last_seen=excluded.last_seen""",
                [(f"{e.venue.value}:{e.event_id}", e.venue.value, e.event_id, e.title,
                  e.category, int(e.mutually_exclusive), int(e.neg_risk), now)
                 for e in rows],
            )
        return len(rows)

    def save_wallet_scores(self, scores: Iterable[Any]) -> int:
        now = datetime.now(timezone.utc).isoformat()
        rows = list(scores)
        with self.connect() as conn:
            conn.executemany(
                """INSERT OR REPLACE INTO wallet_scores
                   (address, ts, username, category, n_resolved, entry_adj_edge,
                    t_stat, brier, herfindahl, volume_to_pnl, is_mm, qualified,
                    disqualified_for)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [(s.address, now, s.username, s.category, s.n_resolved,
                  s.entry_adjusted_edge, s.t_stat, s.brier, s.pnl_herfindahl,
                  None if s.volume_to_pnl == float("inf") else s.volume_to_pnl,
                  int(s.is_market_maker), int(s.qualified),
                  json.dumps(s.disqualified_for)) for s in rows],
            )
        return len(rows)

    # --------------------------------------------------------------- signals

    def save_signal(self, signal: Signal) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO signals (ts, strategy, venue, market_id, title, side,
                       entry_price, est_probability, edge, confidence,
                       days_to_resolution, score, deterministic, category,
                       stake, contracts, legs, rationale, counter_case)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (signal.ts.isoformat(), signal.strategy, signal.venue.value,
                 signal.market_id, signal.title, signal.side, signal.entry_price,
                 signal.est_probability, signal.edge, signal.confidence,
                 signal.days_to_resolution, signal.score, int(signal.deterministic),
                 signal.category, signal.stake, signal.contracts,
                 json.dumps([leg.__dict__ for leg in signal.legs], default=str),
                 json.dumps(signal.rationale, default=str), signal.counter_case),
            )
            signal_id = cur.lastrowid
            # Journal EVERY signal at issue time, taken or not. Logging untaken
            # alerts is what separates "the model is bad" from "the model is fine
            # and execution is bad" - opposite problems, opposite fixes.
            conn.execute(
                "INSERT OR IGNORE INTO journal (signal_id, alerted_ts) VALUES (?,?)",
                (signal_id, signal.ts.isoformat()),
            )
        return signal_id

    def mark_alerted(self, signal_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE signals SET alerted=1 WHERE id=?", (signal_id,))

    def recent_signal_exists(self, strategy: str, market_id: str,
                             within_hours: float = 12.0) -> bool:
        """Suppress duplicate alerts for the same opportunity."""
        with self.connect() as conn:
            row = conn.execute(
                """SELECT 1 FROM signals
                   WHERE strategy=? AND market_id=?
                     AND ts > datetime('now', ?) LIMIT 1""",
                (strategy, market_id, f"-{within_hours} hours"),
            ).fetchone()
        return row is not None

    def log_cross_venue(self, ts, kalshi_id, poly_id, title, gross, net,
                        depth) -> None:
        """Observe-only. Never alerts, never sizes - just builds the dataset that
        will eventually say whether cross-venue arb at this bankroll was real."""
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO cross_venue_log (ts, kalshi_id, poly_id, title,
                       gross_spread, net_after_fees, depth_available, confirmed)
                   VALUES (?,?,?,?,?,?,?,0)""",
                (ts, kalshi_id, poly_id, title, gross, net, depth),
            )

    # ----------------------------------------------------------- calibration

    def resolve_signal(self, signal_id: int, outcome: float,
                       pnl: Optional[float] = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE journal SET outcome=?, pnl=?, resolved_ts=?
                   WHERE signal_id=?""",
                (outcome, pnl, datetime.now(timezone.utc).isoformat(), signal_id),
            )

    def resolved_predictions(self) -> list[tuple[float, float]]:
        """(predicted_probability, realized_outcome) for every resolved signal."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.est_probability, j.outcome
                   FROM signals s JOIN journal j ON j.signal_id = s.id
                   WHERE j.outcome IS NOT NULL"""
            ).fetchall()
        return [(r[0], r[1]) for r in rows if r[0] is not None]

    def qualified_wallets(self, max_age_hours: float = 24.0) -> list[str]:
        """Addresses that passed every screen on the most recent scoring run."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT address, MAX(ts) FROM wallet_scores
                   WHERE qualified = 1 AND ts > datetime('now', ?)
                   GROUP BY address""",
                (f"-{max_age_hours} hours",),
            ).fetchall()
        return [r[0] for r in rows]

    def wallet_score_age_hours(self) -> Optional[float]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT (julianday('now') - julianday(MAX(ts))) * 24 "
                "FROM wallet_scores"
            ).fetchone()
        return row[0] if row and row[0] is not None else None

    # ------------------------------------------------------------ bot state

    def set_state(self, chat_id: str, key: str, value) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO bot_state (chat_id, key, value, updated)
                   VALUES (?,?,?,?)
                   ON CONFLICT(chat_id, key) DO UPDATE SET
                       value=excluded.value, updated=excluded.updated""",
                (str(chat_id), key, json.dumps(value),
                 datetime.now(timezone.utc).isoformat()),
            )

    def get_state(self, chat_id: str, key: str, default=None):
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM bot_state WHERE chat_id=? AND key=?",
                (str(chat_id), key),
            ).fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return default

    def signal_by_id(self, signal_id: int) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT s.*, j.taken, j.outcome, j.actual_entry, j.pnl
                   FROM signals s LEFT JOIN journal j ON j.signal_id = s.id
                   WHERE s.id = ?""",
                (signal_id,),
            ).fetchone()
        return dict(row) if row else None

    def record_decision(self, signal_id: int, taken: bool,
                        actual_entry: Optional[float] = None,
                        stake: Optional[float] = None,
                        notes: str = "") -> None:
        """Log whether the operator acted.

        Logging PASSES matters as much as logging takes: it is the only way to
        separate "the model is bad" from "the model is fine and I am not
        executing it" - opposite problems with opposite fixes.
        """
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO journal (signal_id, alerted_ts, taken,
                       actual_entry, stake, notes)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(signal_id) DO UPDATE SET
                       taken=excluded.taken,
                       actual_entry=COALESCE(excluded.actual_entry, journal.actual_entry),
                       stake=COALESCE(excluded.stake, journal.stake),
                       notes=excluded.notes""",
                (signal_id, datetime.now(timezone.utc).isoformat(), int(taken),
                 actual_entry, stake, notes),
            )

    def pnl_by_strategy(self, since_days: float = 7.0) -> list[dict]:
        """Which edge is actually paying. Kill what is not."""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT s.strategy,
                          COUNT(*)                                    AS alerted,
                          SUM(CASE WHEN j.taken=1 THEN 1 ELSE 0 END)  AS taken,
                          SUM(CASE WHEN j.outcome=1 THEN 1 ELSE 0 END) AS wins,
                          SUM(CASE WHEN j.outcome=0 THEN 1 ELSE 0 END) AS losses,
                          COALESCE(SUM(j.pnl), 0)                     AS pnl
                   FROM signals s LEFT JOIN journal j ON j.signal_id = s.id
                   WHERE s.ts > datetime('now', ?)
                   GROUP BY s.strategy ORDER BY pnl DESC""",
                (f"-{since_days} days",),
            ).fetchall()
        return [dict(r) for r in rows]

    def scorecard(self) -> dict:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT
                     COUNT(*)                                   AS alerted,
                     SUM(CASE WHEN taken=1 THEN 1 ELSE 0 END)   AS taken,
                     SUM(CASE WHEN taken=0 THEN 1 ELSE 0 END)   AS passed,
                     SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved,
                     COALESCE(SUM(pnl), 0)                      AS pnl
                   FROM journal"""
            ).fetchone()
        return dict(row) if row else {}

    def recent_signals(self, limit: int = 25) -> list[dict]:
        """Full rows. The bot rehydrates Signal objects from these and maps
        briefing positions back to ids, so every column must be present."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM signals ORDER BY ts DESC, score DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def latest_wallet_score(self, address: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute(
                """SELECT username, category, n_resolved, entry_adj_edge, t_stat,
                          brier, herfindahl, volume_to_pnl, qualified
                   FROM wallet_scores WHERE address=? ORDER BY ts DESC LIMIT 1""",
                (address,),
            ).fetchone()
        if not row or not row[8]:
            return None
        return {
            "username": row[0], "category": row[1], "n_resolved": row[2],
            "entry_adj_edge": row[3], "t_stat": row[4], "brier": row[5],
            "herfindahl": row[6], "volume_to_pnl": row[7],
        }

    def counts(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("markets", "market_snapshots", "events", "signals",
                          "wallet_scores", "cross_venue_log")
            }
