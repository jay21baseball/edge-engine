"""Whale tracker: pull every trade a tracked wallet makes, the moment it lands.

Polymarket settles on-chain, so every trade is public. The /activity feed lists
a wallet's trades newest-first with the dollar amount, price, exact outcome, and
a transaction hash. Poll it on a tight loop, remember which hashes were already
seen, and anything new is a trade the whale just made - alerted instantly.

The alert is written to read like a text from a friend who happens to watch
these wallets, not a machine report: plain words, plain numbers, no boxes or
symbols. That is a deliberate choice, not an accident of formatting.

One honest caveat baked into the wording: seeing a whale's trade is not the same
as being able to copy it. Their fill already moved the price, and you cannot see
the other side of their book (a hedge on Kalshi, a sportsbook, an equity). The
alert says what they did and leaves the judgement to you.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)


@dataclass
class Whale:
    name: str
    address: str
    username: str = ""                 # for the polymarket.com/@ link
    min_usdc: float = 0.0              # ignore trades smaller than this
    bot_token: Optional[str] = None    # a dedicated bot for this whale, optional
    chat_id: Optional[str] = None

    @property
    def key(self) -> str:
        return self.address.lower()

    @property
    def link(self) -> str:
        if self.username:
            return f"polymarket.com/@{self.username}"
        return f"polymarket.com/profile/{self.address}"


def load_whales(config: dict) -> list[Whale]:
    out = []
    for w in (config.get("whales") or []):
        if not w.get("address"):
            continue
        out.append(Whale(
            name=w.get("name") or w["address"][:10],
            address=str(w["address"]),
            username=w.get("username", ""),
            min_usdc=float(w.get("min_usdc", 0)),
            bot_token=w.get("bot_token"),
            chat_id=str(w.get("chat_id")) if w.get("chat_id") else None,
        ))
    return out


def _cents(price: float) -> str:
    return f"{price * 100:.0f}c"


def _money(usdc: float) -> str:
    return f"${usdc:,.0f}"


def _clock(ts: int, tz_name: str = "America/New_York") -> str:
    try:
        when = datetime.fromtimestamp(ts, tz=ZoneInfo(tz_name))
    except Exception:
        when = datetime.fromtimestamp(ts, tz=timezone.utc)
    # Build "1:05 PM" portably - %-I (Linux) and %#I (Windows) are not
    # cross-platform, so format the hour by hand.
    hour = when.hour % 12 or 12
    return f"{hour}:{when.minute:02d} {when.strftime('%p')}"


def _size_word(usdc: float) -> str:
    """A plain read on how big this bet is for the whale watcher."""
    if usdc >= 10000:
        return "Big one"
    if usdc >= 2000:
        return "Decent size"
    if usdc >= 500:
        return "Worth a look"
    return "Small one"


def format_trade(whale: Whale, trade: dict, portfolio: Optional[float] = None,
                 tz_name: str = "America/New_York") -> str:
    """A single trade, written like a friend telling you what they just saw."""
    side = trade["side"].upper()
    verb = "bought" if side == "BUY" else "sold"
    outcome = trade["outcome"] or "a position"
    market = trade["title"] or ""
    usdc = trade["usdc"]
    price = trade["price"]

    lines = [
        f"{whale.name} just {verb} something.",
        "",
        f"{_size_word(usdc)}: {verb} {_money(usdc)} of {outcome} at {_cents(price)}.",
    ]
    if market and market.lower() not in outcome.lower():
        lines.append(f"Market: {market}")
    lines.append(f"That is {trade['size']:,.0f} shares, around "
                 f"{_clock(trade['ts'], tz_name)}.")
    if portfolio:
        lines += ["", f"His book is worth about {_money(portfolio)} right now."]
    lines += [
        "",
        f"{whale.link}",
        "",
        "Heads up though: his fill already moved the price, and you cannot see "
        "the other side of his book. Treat it as a tip to go look, not a green "
        "light.",
    ]
    return "\n".join(lines)


class WhaleTracker:
    """Detects new trades per wallet and hands back formatted alerts."""

    def __init__(self, poly, store):
        self.poly = poly
        self.store = store

    def _seen_key(self, whale: Whale) -> str:
        return f"whale_seen:{whale.key}"

    def check(self, whale: Whale, prime: bool = False) -> list[str]:
        """Return alert texts for trades not seen before.

        On the very first check for a wallet (prime=True, or no history yet) it
        records the current trades WITHOUT alerting - otherwise every restart
        would replay the whale's recent history as a burst of fake 'just now'
        alerts.
        """
        try:
            trades = self.poly.recent_trades(whale.address, limit=40)
        except Exception as e:
            log.warning("whale %s activity fetch failed: %s", whale.name, e)
            return []
        if not trades:
            return []

        seen = set(self.store.get_state("_system", self._seen_key(whale), []) or [])
        first_run = not seen
        new = [t for t in trades if t["hash"] and t["hash"] not in seen
               and t["usdc"] >= whale.min_usdc]

        # Always advance the seen set to the latest hashes.
        all_hashes = [t["hash"] for t in trades if t["hash"]]
        self.store.set_state("_system", self._seen_key(whale),
                             (list(seen) + all_hashes)[-400:])
        self.store.log_whale_trades(whale.name, whale.address, new)

        if prime or first_run:
            log.info("whale %s primed with %d recent trades (no alert)",
                     whale.name, len(trades))
            return []

        portfolio = None
        if new:
            portfolio = self.poly.portfolio_value(whale.address)
        # Oldest first so alerts arrive in the order the whale made them.
        return [format_trade(whale, t, portfolio) for t in reversed(new)]
