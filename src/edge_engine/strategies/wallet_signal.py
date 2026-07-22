"""Wallet skill screening and the attention queue.

This is NOT copy trading. It is an attention filter.

The public leaderboard ranks by absolute P&L, which selects for bankroll size and
variance rather than skill. Measured live on 2026-07-21, roughly half the top 25
wallets carry volume-to-P&L ratios between 35:1 and 155:1 - the signature of
high-frequency market making, whose positions are inventory they are paying to
shed. Mirroring them means volunteering to be their exit liquidity. Meanwhile the
rank-3 wallet showed $3.66M profit on only $2.42M of volume, a sub-1:1 ratio that
means a handful of longshots hit: variance wearing a crown.

Every screen below exists to exclude one of those two failure modes.

Output is a shortlist of markets worth a human's attention, each annotated with how
much of the move already happened - because you are always entering after them, at
a worse price, and that number is usually the reason not to take the trade.
"""
from __future__ import annotations

import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..ingest.models import (
    LeaderboardEntry,
    Side,
    Venue,
    WalletActivity,
    WalletPosition,
)
from .base import Signal, Strategy

log = logging.getLogger(__name__)

MIN_RESOLVED_POSITIONS = 30
MM_VOLUME_PNL_RATIO = 20.0
MAX_PNL_HERFINDAHL = 0.40
RECENCY_HALF_LIFE_DAYS = 90.0
MIN_ENTRY_ADJUSTED_EDGE = 0.02
MIN_T_STAT = 1.5

# Too good to be true means you are measuring the wrong thing.
#
# A sustained entry-adjusted edge above ~15% does not exist in a liquid market:
# it would mean systematically buying at 40c what resolves at 55c, forever,
# against professional counterparties. Measured on a correctly-sampled
# chronological history, a genuinely sharp wallet lands near +0.035.
#
# Every time this ceiling has tripped during development it was a data artifact,
# never a trader: first `/closed-positions` defaulting to a wallet's best-50
# trades, then unpaginated samples. Flagging it is the same discipline as the
# market-maker screen - an implausible number is a bug report, not a signal.
MAX_PLAUSIBLE_EDGE = 0.15


@dataclass
class WalletScore:
    address: str
    username: str = ""
    n_resolved: int = 0
    entry_adjusted_edge: float = 0.0
    t_stat: float = 0.0
    brier: float = 1.0
    pnl_herfindahl: float = 1.0
    volume_to_pnl: float = float("inf")
    is_market_maker: bool = False
    two_sided_markets: int = 0
    recency_weight: float = 0.0
    category: str = "OVERALL"
    qualified: bool = False
    disqualified_for: list[str] = field(default_factory=list)

    @property
    def composite(self) -> float:
        """Ranking score among already-qualified wallets."""
        if not self.qualified:
            return 0.0
        return (
            self.entry_adjusted_edge
            * min(self.t_stat / 3.0, 1.0)
            * (1.0 - self.brier)
            * (1.0 - self.pnl_herfindahl)
            * self.recency_weight
        )


def _is_resolved(p: WalletPosition) -> bool:
    return p.redeemable or p.current_price <= 0.02 or p.current_price >= 0.98


def _outcome(p: WalletPosition) -> float:
    return 1.0 if p.current_price >= 0.5 else 0.0


def detect_market_maker(
    entry: Optional[LeaderboardEntry],
    positions: list[WalletPosition],
    activity: list[WalletActivity],
) -> tuple[bool, int, list[str]]:
    """Hard exclusion. Three independent tells, any one is disqualifying.

    Market makers are the single most dangerous thing to copy: their position is
    inventory they want gone, so mirroring it means taking the exact side they are
    paying to offload.
    """
    reasons: list[str] = []

    ratio = entry.volume_to_pnl if entry else float("inf")
    if entry and ratio > MM_VOLUME_PNL_RATIO:
        reasons.append(f"volume/pnl {ratio:.0f}:1 exceeds {MM_VOLUME_PNL_RATIO:.0f}:1")

    # Holding BOTH sides of the same market is inventory management, not a view.
    sides_by_market: dict[str, set] = defaultdict(set)
    for p in positions:
        if p.size > 0:
            sides_by_market[p.market_id].add(p.side)
    two_sided = sum(1 for s in sides_by_market.values() if len(s) > 1)
    if two_sided >= 3:
        reasons.append(f"two-sided in {two_sided} markets")

    # Round-tripping the same market repeatedly within the activity window.
    if activity:
        per_market = defaultdict(lambda: {"BUY": 0, "SELL": 0})
        for a in activity:
            if a.side in ("BUY", "SELL"):
                per_market[a.market_id][a.side] += 1
        round_trippers = sum(
            1 for c in per_market.values() if c["BUY"] >= 2 and c["SELL"] >= 2
        )
        if round_trippers >= 5:
            reasons.append(f"round-trips {round_trippers} markets")

    return bool(reasons), two_sided, reasons


def score_wallet(
    address: str,
    entry: Optional[LeaderboardEntry],
    positions: list[WalletPosition],
    activity: list[WalletActivity],
    now: Optional[datetime] = None,
) -> WalletScore:
    now = now or datetime.now(timezone.utc)
    score = WalletScore(
        address=address.lower(),
        username=entry.username if entry else "",
        category=entry.category if entry else "OVERALL",
        volume_to_pnl=entry.volume_to_pnl if entry else float("inf"),
    )

    is_mm, two_sided, mm_reasons = detect_market_maker(entry, positions, activity)
    score.is_market_maker = is_mm
    score.two_sided_markets = two_sided
    if is_mm:
        score.disqualified_for.extend(mm_reasons)

    resolved = [p for p in positions if _is_resolved(p) and 0 < p.avg_price < 1]
    score.n_resolved = len(resolved)
    if score.n_resolved < MIN_RESOLVED_POSITIONS:
        score.disqualified_for.append(
            f"only {score.n_resolved} resolved positions (need {MIN_RESOLVED_POSITIONS})"
        )

    if resolved:
        # THE core metric. A trader who buys 90c favorites and wins 90% of the
        # time has ZERO skill - they are exactly paying fair value. Only the
        # excess over the price they paid is evidence of anything, and the public
        # leaderboard measures none of it.
        edges = [_outcome(p) - p.avg_price for p in resolved]
        score.entry_adjusted_edge = statistics.fmean(edges)
        if len(edges) > 1:
            sd = statistics.pstdev(edges)
            score.t_stat = (
                score.entry_adjusted_edge / (sd / math.sqrt(len(edges)))
                if sd > 1e-9 else 0.0
            )

        # Brier over their implied forecasts: lower is better calibrated.
        score.brier = statistics.fmean(
            [(p.avg_price - _outcome(p)) ** 2 for p in resolved]
        )

        # Concentration: if one trade carries the P&L, that is a lottery ticket.
        gains = [max(p.cash_pnl, 0.0) for p in resolved]
        total = sum(gains)
        score.pnl_herfindahl = (
            sum((g / total) ** 2 for g in gains) if total > 0 else 1.0
        )

    if activity:
        # Exponential recency decay: sharp on 2024 elections != sharp now.
        ages = [(now - a.ts).total_seconds() / 86400.0 for a in activity]
        score.recency_weight = statistics.fmean(
            [0.5 ** (max(age, 0.0) / RECENCY_HALF_LIFE_DAYS) for age in ages]
        )

    if score.entry_adjusted_edge < MIN_ENTRY_ADJUSTED_EDGE:
        score.disqualified_for.append(
            f"entry-adjusted edge {score.entry_adjusted_edge:+.3f} "
            f"below {MIN_ENTRY_ADJUSTED_EDGE:+.3f}"
        )
    elif score.entry_adjusted_edge > MAX_PLAUSIBLE_EDGE:
        score.disqualified_for.append(
            f"IMPLAUSIBLE edge {score.entry_adjusted_edge:+.3f} exceeds "
            f"{MAX_PLAUSIBLE_EDGE:+.3f} - treat as a data-quality fault, not "
            f"skill. Check the closed-positions sample is chronological and "
            f"fully paginated."
        )
    if score.t_stat < MIN_T_STAT:
        score.disqualified_for.append(f"t-stat {score.t_stat:.2f} below {MIN_T_STAT}")
    if score.pnl_herfindahl > MAX_PNL_HERFINDAHL:
        score.disqualified_for.append(
            f"P&L concentration {score.pnl_herfindahl:.2f} exceeds {MAX_PNL_HERFINDAHL}"
        )

    score.qualified = not score.disqualified_for
    return score


class WalletAttentionQueue(Strategy):
    """Surfaces markets where multiple qualified wallets agree.

    Deliberately NOT a trade signal. `est_probability` is the crowd's current
    price - the system claims no forecast of its own here. It claims only that a
    market is worth a human's time.
    """

    name = "wallet_attention"
    min_bankroll = 0.0

    def __init__(self, min_agreeing_wallets: int = 2,
                 recency_window_hours: float = 48.0,
                 max_edge_decay: float = 0.60):
        self.min_agreeing = min_agreeing_wallets
        self.recency_window = timedelta(hours=recency_window_hours)
        self.max_edge_decay = max_edge_decay

    def build(
        self,
        scores: dict[str, WalletScore],
        positions_by_wallet: dict[str, list[WalletPosition]],
        current_prices: dict[str, float],
        now: Optional[datetime] = None,
        days_to_resolution: Optional[dict[str, float]] = None,
    ) -> list[Signal]:
        """`current_prices` are YES prices keyed by conditionId.

        The NO price is derived as (1 - yes) per market, because a wallet holding
        NO must be compared against the NO price. Comparing a NO entry against a
        YES quote produced edges of +44% and "move already gone" figures of
        -683%, which is how that bug announced itself.
        """
        now = now or datetime.now(timezone.utc)
        qualified = {a: s for a, s in scores.items() if s.qualified}
        if not qualified:
            return []

        grouped: dict[tuple[str, Side], list[tuple[WalletScore, WalletPosition]]] = \
            defaultdict(list)
        for address, score in qualified.items():
            for pos in positions_by_wallet.get(address, []):
                if pos.size <= 0 or _is_resolved(pos):
                    continue
                if now - pos.observed_ts > self.recency_window:
                    continue
                grouped[(pos.market_id, pos.side)].append((score, pos))

        signals: list[Signal] = []
        for (market_id, side), members in grouped.items():
            if len(members) < self.min_agreeing:
                continue

            yes_price = current_prices.get(market_id)
            if yes_price is None or not (0 < yes_price < 1):
                continue
            # Price the side they actually hold.
            current = yes_price if side is Side.YES else 1.0 - yes_price
            if not (0 < current < 1):
                continue

            avg_entry = statistics.fmean([p.avg_price for _, p in members])
            title = members[0][1].title

            # How much of their edge is already gone. You are ALWAYS entering
            # after them at a worse price; this is the number that most often
            # says do not bother, and it is shown on every alert.
            headroom = 1.0 - avg_entry
            captured = (current - avg_entry) / headroom if headroom > 1e-6 else 1.0
            # Clamp: a negative value means the price moved AGAINST them, which
            # is not "negative decay" - none of their edge has been captured yet.
            captured = min(max(captured, 0.0), 1.0)
            remaining = max(0.0, 1.0 - captured)
            if captured >= self.max_edge_decay:
                log.debug("%s: %.0f%% of move already gone, suppressed",
                          title[:40], captured * 100)
                continue

            wallet_edge = statistics.fmean(
                [s.entry_adjusted_edge for s, _ in members]
            )
            confidence = min(
                1.0,
                statistics.fmean([s.composite for s, _ in members]) * 10.0,
            ) * remaining

            signals.append(Signal(
                strategy=self.name,
                venue=Venue.POLYMARKET,
                market_id=market_id,
                title=title,
                side=side.value,
                entry_price=current,
                est_probability=current,   # no independent forecast is claimed
                edge=wallet_edge * remaining,
                confidence=confidence,
                # Real horizon, not a placeholder. A 2028 election market ranked
                # as if it resolved in a week defeats the whole edge-per-day
                # ranking and floods the queue with capital that would be locked
                # up for years.
                days_to_resolution=(days_to_resolution or {}).get(market_id, 30.0),
                deterministic=False,
                # No independent forecast is claimed, so this is never sized.
                # It is a prompt to go look, not an instruction to buy.
                advisory=True,
                rationale={
                    "agreeing_wallets": len(members),
                    "wallets": [
                        {
                            "user": s.username or s.address[:10],
                            "entry": round(p.avg_price, 4),
                            "size": round(p.size, 1),
                            "entry_adj_edge": round(s.entry_adjusted_edge, 4),
                            "n_resolved": s.n_resolved,
                            "vol_pnl": (round(s.volume_to_pnl, 1)
                                        if s.volume_to_pnl != float("inf") else None),
                        }
                        for s, p in members
                    ],
                    "their_avg_entry": round(avg_entry, 4),
                    "current_price": round(current, 4),
                    "move_already_captured_pct": round(captured * 100, 1),
                    "edge_remaining_pct": round(remaining * 100, 1),
                },
                counter_case=(
                    f"You would enter at {current:.2f} vs their {avg_entry:.2f} - "
                    f"{captured * 100:.0f}% of the move is already gone. You cannot "
                    f"see their other leg: this position may be hedged on Kalshi, in "
                    f"a sportsbook, or against an equity. Attention prompt only - "
                    f"form your own view before acting."
                ),
            ))

        return sorted(signals, key=lambda s: -s.score)
