"""Which opportunities are worth interrupting you for.

The daily briefing is pull — you ask, it answers. This is push: something good
enough appeared that waiting until tomorrow would cost you.

The hard part is not detection, it is restraint. An alerter that fires on
everything trains you to ignore it, and then it fires on the one that mattered
and you swipe it away with the rest. So every rule here is a bar to clear, not
a filter to pass, and there are hard daily caps regardless of how good the day
looks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timezone
from typing import Optional

from ..strategies.base import Signal


@dataclass
class AlertRule:
    """The bar a signal must clear to be worth a push notification."""

    name: str
    min_edge: float
    min_confidence: float = 0.0
    min_score: float = 0.0            # edge per day of capital lockup
    max_days_to_resolution: float = 10_000.0
    label: str = ""

    def clears(self, signal: Signal) -> bool:
        return (
            signal.edge >= self.min_edge
            and signal.confidence >= self.min_confidence
            and signal.score >= self.min_score
            and signal.days_to_resolution <= self.max_days_to_resolution
        )


# Per-strategy bars. A locked arb and a forecast are not comparable: one is
# arithmetic, the other is an estimate that can be wrong, so the forecast has to
# clear a far higher bar to earn the same interruption.
DEFAULT_RULES: dict[str, AlertRule] = {
    "combinatorial_arb": AlertRule(
        "combinatorial_arb", min_edge=0.01, min_confidence=0.0, min_score=0.002,
        label="Locked arbitrage — arithmetic, not a forecast",
    ),
    "sportsbook_divergence": AlertRule(
        "sportsbook_divergence", min_edge=0.06, min_confidence=0.55,
        min_score=0.02, max_days_to_resolution=3.0,
        label="Stale line — books have moved, this market has not",
    ),
    "wallet_attention": AlertRule(
        "wallet_attention", min_edge=0.10, min_confidence=0.65, min_score=0.01,
        max_days_to_resolution=30.0,
        label="Sharp money concentration",
    ),
    "weather": AlertRule(
        "weather", min_edge=0.08, min_confidence=0.6, min_score=0.03,
        max_days_to_resolution=2.0,
        label="Forecast disagrees with the market",
    ),
    "favorite_longshot": AlertRule(
        "favorite_longshot", min_edge=0.05, min_confidence=0.6, min_score=0.01,
        label="Structural mispricing",
    ),
}

FALLBACK_RULE = AlertRule("default", min_edge=0.10, min_confidence=0.6,
                          min_score=0.02, label="High edge")

# Only something resolving inside this window is allowed to wake you.
URGENT_HORIZON_DAYS = 0.5


@dataclass
class AlertPolicy:
    """Caps and quiet hours. These exist to protect the signal-to-noise ratio."""

    max_per_day: int = 5
    quiet_start_hour: int = 23
    quiet_end_hour: int = 7
    respect_quiet_hours: bool = True
    cooldown_hours: float = 6.0        # per market, not global
    rules: dict[str, AlertRule] = field(default_factory=lambda: dict(DEFAULT_RULES))

    def rule_for(self, strategy: str) -> AlertRule:
        return self.rules.get(strategy, FALLBACK_RULE)

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        if not self.respect_quiet_hours:
            return False
        now = now or datetime.now(timezone.utc)
        hour = now.hour
        if self.quiet_start_hour <= self.quiet_end_hour:
            return self.quiet_start_hour <= hour < self.quiet_end_hour
        # Window wraps midnight (e.g. 23:00 -> 07:00).
        return hour >= self.quiet_start_hour or hour < self.quiet_end_hour


@dataclass
class AlertDecision:
    signal: Signal
    rule: AlertRule
    urgent: bool
    reason: str


class Alerter:
    """Decides what interrupts you, and refuses to interrupt you too often."""

    def __init__(self, store, policy: Optional[AlertPolicy] = None):
        self.store = store
        self.policy = policy or AlertPolicy()

    # ------------------------------------------------------------- selection

    def select(self, signals: list[Signal], chat_id: str,
               now: Optional[datetime] = None) -> list[AlertDecision]:
        now = now or datetime.now(timezone.utc)
        sent_today = self._count_sent_today(chat_id, now)
        remaining = max(0, self.policy.max_per_day - sent_today)
        if remaining == 0:
            return []

        recent = set(self.store.get_state(chat_id, "alerted_markets", []) or [])
        decisions: list[AlertDecision] = []

        for signal in sorted(signals, key=lambda s: -s.score):
            if len(decisions) >= remaining:
                break
            # Advisory signals are research prompts. They belong in the daily
            # briefing, never in a push notification - "go read about this" is
            # not worth a buzz.
            if signal.advisory:
                continue
            rule = self.policy.rule_for(signal.strategy)
            if not rule.clears(signal):
                continue
            key = f"{signal.strategy}:{signal.market_id}"
            if key in recent:
                continue

            # A locked arb is time-sensitive in a way a forecast is not: the
            # spread closes when someone else takes it. Those pierce quiet
            # hours, as does anything resolving inside 12 hours. A game
            # tomorrow is NOT urgent - the line will still be there at 7am,
            # and waking someone for it is how an alerter loses its authority.
            urgent = (signal.deterministic
                      or signal.days_to_resolution <= URGENT_HORIZON_DAYS)
            if self.policy.in_quiet_hours(now) and not urgent:
                continue

            decisions.append(AlertDecision(
                signal=signal, rule=rule, urgent=urgent,
                reason=self._reason(signal, rule),
            ))
        return decisions

    @staticmethod
    def _reason(signal: Signal, rule: AlertRule) -> str:
        margin = (signal.edge - rule.min_edge) * 100
        return (f"{rule.label}. Edge {signal.edge * 100:.1f}% is "
                f"{margin:.1f} points above the {rule.min_edge * 100:.0f}% bar "
                f"for this strategy.")

    def record_sent(self, chat_id: str, decisions: list[AlertDecision],
                    now: Optional[datetime] = None) -> None:
        now = now or datetime.now(timezone.utc)
        log = self.store.get_state(chat_id, "alert_log", []) or []
        recent = self.store.get_state(chat_id, "alerted_markets", []) or []
        for decision in decisions:
            log.append(now.isoformat())
            recent.append(f"{decision.signal.strategy}:"
                          f"{decision.signal.market_id}")
        self.store.set_state(chat_id, "alert_log", log[-200:])
        self.store.set_state(chat_id, "alerted_markets", recent[-300:])

    def _count_sent_today(self, chat_id: str, now: datetime) -> int:
        log = self.store.get_state(chat_id, "alert_log", []) or []
        today = now.date().isoformat()
        return sum(1 for stamp in log if str(stamp).startswith(today))


def format_alert(decision: AlertDecision) -> str:
    """A push worth interrupting for, written plainly like a quick heads-up."""
    from .format import american_str, cents, horizon, money

    signal = decision.signal
    opener = ("Locked arb just showed up." if signal.deterministic
              else "Something big just cleared the bar.")

    lines = [
        opener, "",
        signal.title[:75], "",
    ]
    act = signal.side.replace("_", " ").upper()
    if signal.deterministic:
        lines.append(f"Buy {act} on {signal.venue.value.title()}. "
                     f"Guaranteed {signal.edge * 100:+.1f}%, no guessing.")
    else:
        lines.append(f"Bet {act} on {signal.venue.value.title()} at "
                     f"{american_str(signal.entry_price)} "
                     f"({cents(signal.entry_price)}). About "
                     f"{signal.edge * 100:+.1f}% of edge.")
    if signal.stake:
        lines.append(f"Size it around {money(signal.stake)}.")
    lines.append(f"Settles {horizon(signal.days_to_resolution)}.")

    if signal.counter_case:
        lines += ["", f"The catch: {signal.counter_case[:180]}"]
    lines += ["", "Hit /dailyedge for the full read."]
    return "\n".join(lines)
