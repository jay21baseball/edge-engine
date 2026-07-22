"""Postgres storage for cloud runs (Supabase free tier).

Implements the same surface as SqliteStore. This is the seam paying off: no
strategy, sizing, or alert code changes when the engine moves off your PC.

Chosen via config `store: postgres` plus `database_url`, or the DATABASE_URL
environment variable (which is how the GitHub Actions workflow injects it).
"""
from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ..ingest.models import Market
from ..strategies.base import Signal

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    key TEXT PRIMARY KEY, venue TEXT, market_id TEXT, event_id TEXT,
    title TEXT, category TEXT, close_ts TIMESTAMPTZ, status TEXT,
    fee_rate DOUBLE PRECISION, first_seen TIMESTAMPTZ, last_seen TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS market_snapshots (
    id BIGSERIAL PRIMARY KEY, key TEXT, ts TIMESTAMPTZ,
    yes_bid DOUBLE PRECISION, yes_ask DOUBLE PRECISION,
    no_bid DOUBLE PRECISION, no_ask DOUBLE PRECISION,
    volume DOUBLE PRECISION, liquidity DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS ix_snap_key_ts ON market_snapshots(key, ts);

CREATE TABLE IF NOT EXISTS events (
    key TEXT PRIMARY KEY, venue TEXT, event_id TEXT, title TEXT, category TEXT,
    mutually_exclusive BOOLEAN, neg_risk BOOLEAN, last_seen TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS wallet_scores (
    address TEXT, ts TIMESTAMPTZ, username TEXT, category TEXT,
    n_resolved INTEGER, entry_adj_edge DOUBLE PRECISION,
    t_stat DOUBLE PRECISION, brier DOUBLE PRECISION,
    herfindahl DOUBLE PRECISION, volume_to_pnl DOUBLE PRECISION,
    is_mm BOOLEAN, qualified BOOLEAN, disqualified_for TEXT,
    PRIMARY KEY (address, ts)
);
CREATE TABLE IF NOT EXISTS signals (
    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, strategy TEXT, venue TEXT,
    market_id TEXT, title TEXT, side TEXT, entry_price DOUBLE PRECISION,
    est_probability DOUBLE PRECISION, edge DOUBLE PRECISION,
    confidence DOUBLE PRECISION, days_to_resolution DOUBLE PRECISION,
    score DOUBLE PRECISION, deterministic BOOLEAN, category TEXT,
    stake DOUBLE PRECISION, contracts DOUBLE PRECISION,
    legs TEXT, rationale TEXT, counter_case TEXT, alerted BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS ix_signals_ts ON signals(ts);

CREATE TABLE IF NOT EXISTS journal (
    signal_id BIGINT PRIMARY KEY, alerted_ts TIMESTAMPTZ,
    taken BOOLEAN DEFAULT FALSE, actual_entry DOUBLE PRECISION,
    stake DOUBLE PRECISION, outcome DOUBLE PRECISION, pnl DOUBLE PRECISION,
    resolved_ts TIMESTAMPTZ, notes TEXT
);
CREATE TABLE IF NOT EXISTS bot_state (
    chat_id TEXT, key TEXT, value TEXT, updated TIMESTAMPTZ,
    PRIMARY KEY (chat_id, key)
);

CREATE TABLE IF NOT EXISTS cross_venue_log (
    id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ, kalshi_id TEXT, poly_id TEXT,
    title TEXT, gross_spread DOUBLE PRECISION, net_after_fees DOUBLE PRECISION,
    depth_available DOUBLE PRECISION, confirmed BOOLEAN DEFAULT FALSE
);
"""


class PostgresStore:
    def __init__(self, dsn: str):
        try:
            import psycopg  # noqa: F401
        except ImportError as e:
            raise RuntimeError(
                "psycopg is required for the Postgres store: pip install 'psycopg[binary]'"
            ) from e
        self.dsn = dsn
        with self.connect() as conn:
            with conn.cursor() as cur:
                cur.execute(SCHEMA)

    @contextmanager
    def connect(self):
        import psycopg
        conn = psycopg.connect(self.dsn, connect_timeout=20)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------- ingestion

    def upsert_markets(self, markets: Iterable[Market]) -> int:
        now = datetime.now(timezone.utc)
        rows = list(markets)
        if not rows:
            return 0
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO markets (key, venue, market_id, event_id, title,
                       category, close_ts, status, fee_rate, first_seen, last_seen)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (key) DO UPDATE SET
                       title=EXCLUDED.title, status=EXCLUDED.status,
                       close_ts=EXCLUDED.close_ts, last_seen=EXCLUDED.last_seen""",
                [(m.key, m.venue.value, m.market_id, m.event_id, m.title, m.category,
                  m.close_ts, m.status, m.fee_rate, now, now) for m in rows],
            )
            cur.executemany(
                """INSERT INTO market_snapshots
                       (key, ts, yes_bid, yes_ask, no_bid, no_ask, volume, liquidity)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                [(m.key, now, m.yes_bid, m.yes_ask, m.no_bid, m.no_ask,
                  m.volume, m.liquidity) for m in rows],
            )
        return len(rows)

    def upsert_events(self, events: Iterable[Any]) -> int:
        now = datetime.now(timezone.utc)
        rows = list(events)
        if not rows:
            return 0
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO events (key, venue, event_id, title, category,
                       mutually_exclusive, neg_risk, last_seen)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (key) DO UPDATE SET last_seen=EXCLUDED.last_seen""",
                [(f"{e.venue.value}:{e.event_id}", e.venue.value, e.event_id,
                  e.title, e.category, bool(e.mutually_exclusive),
                  bool(e.neg_risk), now) for e in rows],
            )
        return len(rows)

    def save_wallet_scores(self, scores: Iterable[Any]) -> int:
        now = datetime.now(timezone.utc)
        rows = list(scores)
        if not rows:
            return 0
        with self.connect() as conn, conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO wallet_scores (address, ts, username, category,
                       n_resolved, entry_adj_edge, t_stat, brier, herfindahl,
                       volume_to_pnl, is_mm, qualified, disqualified_for)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (address, ts) DO NOTHING""",
                [(s.address, now, s.username, s.category, s.n_resolved,
                  s.entry_adjusted_edge, s.t_stat, s.brier, s.pnl_herfindahl,
                  None if s.volume_to_pnl == float("inf") else s.volume_to_pnl,
                  bool(s.is_market_maker), bool(s.qualified),
                  json.dumps(s.disqualified_for)) for s in rows],
            )
        return len(rows)

    # --------------------------------------------------------------- signals

    def save_signal(self, signal: Signal) -> int:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO signals (ts, strategy, venue, market_id, title, side,
                       entry_price, est_probability, edge, confidence,
                       days_to_resolution, score, deterministic, category,
                       stake, contracts, legs, rationale, counter_case)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                   RETURNING id""",
                (signal.ts, signal.strategy, signal.venue.value, signal.market_id,
                 signal.title, signal.side, signal.entry_price,
                 signal.est_probability, signal.edge, signal.confidence,
                 signal.days_to_resolution, signal.score, signal.deterministic,
                 signal.category, signal.stake, signal.contracts,
                 json.dumps([leg.__dict__ for leg in signal.legs], default=str),
                 json.dumps(signal.rationale, default=str), signal.counter_case),
            )
            signal_id = cur.fetchone()[0]
            cur.execute(
                """INSERT INTO journal (signal_id, alerted_ts) VALUES (%s,%s)
                   ON CONFLICT (signal_id) DO NOTHING""",
                (signal_id, signal.ts),
            )
        return signal_id

    def mark_alerted(self, signal_id: int) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute("UPDATE signals SET alerted=TRUE WHERE id=%s", (signal_id,))

    def recent_signal_exists(self, strategy: str, market_id: str,
                             within_hours: float = 12.0) -> bool:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT 1 FROM signals WHERE strategy=%s AND market_id=%s
                   AND ts > NOW() - (%s || ' hours')::INTERVAL LIMIT 1""",
                (strategy, market_id, str(within_hours)),
            )
            return cur.fetchone() is not None

    def log_cross_venue(self, ts, kalshi_id, poly_id, title, gross, net,
                        depth) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO cross_venue_log (ts, kalshi_id, poly_id, title,
                       gross_spread, net_after_fees, depth_available, confirmed)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,FALSE)""",
                (ts, kalshi_id, poly_id, title, gross, net, depth),
            )

    # ----------------------------------------------------------- calibration

    def resolve_signal(self, signal_id: int, outcome: float,
                       pnl: Optional[float] = None) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """UPDATE journal SET outcome=%s, pnl=%s, resolved_ts=%s
                   WHERE signal_id=%s""",
                (outcome, pnl, datetime.now(timezone.utc), signal_id),
            )

    def resolved_predictions(self) -> list[tuple[float, float]]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT s.est_probability, j.outcome FROM signals s
                   JOIN journal j ON j.signal_id = s.id
                   WHERE j.outcome IS NOT NULL"""
            )
            return [(r[0], r[1]) for r in cur.fetchall() if r[0] is not None]

    def qualified_wallets(self, max_age_hours: float = 24.0) -> list[str]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT address, MAX(ts) FROM wallet_scores
                   WHERE qualified = TRUE
                     AND ts > NOW() - (%s || ' hours')::INTERVAL
                   GROUP BY address""",
                (str(max_age_hours),),
            )
            return [r[0] for r in cur.fetchall()]

    def wallet_score_age_hours(self) -> Optional[float]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(ts)))/3600 FROM wallet_scores"
            )
            row = cur.fetchone()
            return float(row[0]) if row and row[0] is not None else None

    # ------------------------------------------------------------ bot state

    def set_state(self, chat_id: str, key: str, value) -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO bot_state (chat_id, key, value, updated)
                   VALUES (%s,%s,%s,%s)
                   ON CONFLICT (chat_id, key) DO UPDATE SET
                       value=EXCLUDED.value, updated=EXCLUDED.updated""",
                (str(chat_id), key, json.dumps(value),
                 datetime.now(timezone.utc)),
            )

    def get_state(self, chat_id: str, key: str, default=None):
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT value FROM bot_state WHERE chat_id=%s AND key=%s",
                (str(chat_id), key),
            )
            row = cur.fetchone()
        if not row:
            return default
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return default

    def signal_by_id(self, signal_id: int) -> Optional[dict]:
        cols = ("id", "ts", "strategy", "venue", "market_id", "title", "side",
                "entry_price", "est_probability", "edge", "confidence",
                "days_to_resolution", "score", "deterministic", "category",
                "stake", "contracts", "legs", "rationale", "counter_case")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join('s.' + c for c in cols)}, "
                f"j.taken, j.outcome, j.actual_entry, j.pnl "
                f"FROM signals s LEFT JOIN journal j ON j.signal_id = s.id "
                f"WHERE s.id = %s",
                (signal_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        return dict(zip(cols + ("taken", "outcome", "actual_entry", "pnl"), row))

    def record_decision(self, signal_id: int, taken: bool,
                        actual_entry: Optional[float] = None,
                        stake: Optional[float] = None,
                        notes: str = "") -> None:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """INSERT INTO journal (signal_id, alerted_ts, taken,
                       actual_entry, stake, notes)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (signal_id) DO UPDATE SET
                       taken=EXCLUDED.taken,
                       actual_entry=COALESCE(EXCLUDED.actual_entry,
                                             journal.actual_entry),
                       stake=COALESCE(EXCLUDED.stake, journal.stake),
                       notes=EXCLUDED.notes""",
                (signal_id, datetime.now(timezone.utc), taken, actual_entry,
                 stake, notes),
            )

    def scorecard(self) -> dict:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT COUNT(*),
                          COUNT(*) FILTER (WHERE taken),
                          COUNT(*) FILTER (WHERE NOT taken),
                          COUNT(*) FILTER (WHERE outcome IS NOT NULL),
                          COALESCE(SUM(pnl), 0)
                   FROM journal"""
            )
            row = cur.fetchone()
        keys = ("alerted", "taken", "passed", "resolved", "pnl")
        return dict(zip(keys, row)) if row else {}

    def recent_signals(self, limit: int = 25) -> list[dict]:
        """Full rows. The bot rehydrates Signal objects from these and maps
        briefing positions back to ids, so every column must be present."""
        cols = ("id", "ts", "strategy", "venue", "market_id", "title", "side",
                "entry_price", "est_probability", "edge", "confidence",
                "days_to_resolution", "score", "deterministic", "category",
                "stake", "contracts", "legs", "rationale", "counter_case")
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                f"SELECT {', '.join(cols)} FROM signals "
                f"ORDER BY ts DESC, score DESC LIMIT %s",
                (limit,),
            )
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def latest_wallet_score(self, address: str) -> Optional[dict]:
        with self.connect() as conn, conn.cursor() as cur:
            cur.execute(
                """SELECT username, category, n_resolved, entry_adj_edge, t_stat,
                          brier, herfindahl, volume_to_pnl, qualified
                   FROM wallet_scores WHERE address=%s ORDER BY ts DESC LIMIT 1""",
                (address,),
            )
            row = cur.fetchone()
        if not row or not row[8]:
            return None
        return {
            "username": row[0], "category": row[1], "n_resolved": row[2],
            "entry_adj_edge": row[3], "t_stat": row[4], "brier": row[5],
            "herfindahl": row[6], "volume_to_pnl": row[7],
        }

    def counts(self) -> dict[str, int]:
        out = {}
        with self.connect() as conn, conn.cursor() as cur:
            for table in ("markets", "market_snapshots", "events", "signals",
                          "wallet_scores", "cross_venue_log"):
                cur.execute(f"SELECT COUNT(*) FROM {table}")
                out[table] = cur.fetchone()[0]
        return out
