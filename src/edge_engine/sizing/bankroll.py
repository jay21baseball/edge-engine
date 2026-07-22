"""Bankroll configuration, strategy gating, and the discipline layer.

Everything derives from one number. Change `bankroll` and unit sizes, edge
thresholds, trade caps and strategy availability all move with it.

The counterintuitive rule encoded here: at a SMALL bankroll the minimum edge goes
UP and the trade count goes DOWN. Fixed costs (spread, transfer fees, attention)
are a larger fraction of a small stake, and there is less cushion to survive
variance. Five high-edge trades a week beats thirty marginal ones.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional


# Below this unit size the mechanics stop cooperating: Polymarket rejects orders
# under 5 shares, Kalshi's fee rounds up to a whole cent per order, and the
# absolute profit on a good edge rounds to pocket change. Nothing is forbidden -
# the operator is simply told the truth about what the numbers mean.
PRACTICAL_MIN_UNIT = 25.0


@dataclass
class BankrollConfig:
    bankroll: float = 2500.0
    kelly_fraction: float = 0.25
    max_single_position_pct: float = 5.0
    max_concurrent_exposure_pct: float = 40.0
    min_edge_threshold_pct: float = 4.0
    max_trades_per_day: int = 3
    drawdown_circuit_breaker_pct: float = 15.0

    # Minimum bankroll before a strategy is allowed to size or alert.
    strategy_gates: dict[str, float] = field(default_factory=lambda: {
        "combinatorial_arb": 0.0,
        "wallet_attention": 0.0,
        "weather": 0.0,
        "favorite_longshot": 0.0,
        # Manual two-venue execution means a blown leg is a 5-10% hit at $2.5k
        # but 1-2% at $15k. Runs in observe-only mode below the gate so the
        # operator accumulates real data on whether it was ever worth it.
        "cross_venue_arb": 15000.0,
        "passive_liquidity": 50000.0,
    })

    @property
    def max_concurrent_exposure(self) -> float:
        return self.bankroll * self.max_concurrent_exposure_pct / 100.0

    @property
    def effective_min_edge(self) -> float:
        """Edge floor as a fraction, scaled by bankroll size.

        Small stacks need a higher bar; large stacks can profitably take thinner
        edges because fixed costs shrink relative to position size.
        """
        base = self.min_edge_threshold_pct / 100.0
        if self.bankroll < 1000:
            return base * 1.5
        if self.bankroll < 5000:
            return base
        if self.bankroll < 25000:
            return base * 0.75
        return base * 0.5

    def strategy_enabled(self, name: str) -> bool:
        return self.bankroll >= self.strategy_gates.get(name, 0.0)

    def gate_for(self, name: str) -> float:
        return self.strategy_gates.get(name, 0.0)

    def unit_size(self) -> float:
        """One unit = the standard bet. Reference point, not a cap."""
        return round(self.bankroll * self.max_single_position_pct / 100.0, 2)

    @property
    def is_paper_mode(self) -> bool:
        """True when the unit is too small for the mechanics to work."""
        return self.unit_size() < PRACTICAL_MIN_UNIT

    @property
    def bankroll_for_live(self) -> float:
        """Bankroll needed for a unit to clear the practical minimum."""
        return PRACTICAL_MIN_UNIT / (self.max_single_position_pct / 100.0)

    def expected_profit_per_trade(self, edge: float = 0.05) -> float:
        """What a good edge is actually worth per trade at this size.

        Stated in dollars because percentages hide the problem: 5% sounds fine
        until you see it is nineteen cents.
        """
        return self.unit_size() * edge


@dataclass
class OpenPosition:
    signal_id: int
    market_key: str
    stake: float
    opened: datetime


@dataclass
class DisciplineState:
    """Mechanical rules that decide whether trading happens at all today."""

    config: BankrollConfig
    peak_bankroll: float = 0.0
    current_bankroll: float = 0.0
    trades_today: int = 0
    today: date = field(default_factory=lambda: datetime.now(timezone.utc).date())
    open_positions: list[OpenPosition] = field(default_factory=list)
    week_start_bankroll: float = 0.0
    week_start: date = field(default_factory=lambda: datetime.now(timezone.utc).date())

    def __post_init__(self):
        if self.current_bankroll <= 0:
            self.current_bankroll = self.config.bankroll
        self.peak_bankroll = max(self.peak_bankroll, self.current_bankroll)
        if self.week_start_bankroll <= 0:
            self.week_start_bankroll = self.current_bankroll

    def roll_day(self, today: Optional[date] = None) -> None:
        today = today or datetime.now(timezone.utc).date()
        if today != self.today:
            self.trades_today = 0
            self.today = today
        if today - self.week_start >= timedelta(days=7):
            self.week_start = today
            self.week_start_bankroll = self.current_bankroll

    @property
    def current_exposure(self) -> float:
        return sum(p.stake for p in self.open_positions)

    @property
    def available_capital(self) -> float:
        return max(0.0, self.config.max_concurrent_exposure - self.current_exposure)

    @property
    def weekly_drawdown_pct(self) -> float:
        if self.week_start_bankroll <= 0:
            return 0.0
        drop = self.week_start_bankroll - self.current_bankroll
        return max(0.0, drop / self.week_start_bankroll * 100.0)

    @property
    def circuit_breaker_tripped(self) -> bool:
        return self.weekly_drawdown_pct >= self.config.drawdown_circuit_breaker_pct

    def min_edge_for(self, deterministic: bool = False) -> float:
        """The edge floor, which is not the same bar for both kinds of signal.

        The floor buys two things: a cushion against variance, and a margin for
        being wrong about the probability. A deterministic arb has neither risk -
        the payout is arithmetic - so holding it to the same bar as a forecast
        rejects genuinely free money. It still needs SOME floor to cover
        slippage and execution risk between legs.
        """
        base = self.config.effective_min_edge
        return min(base * 0.25, 0.01) if deterministic else base

    def can_trade(self, strategy: str = "", edge: float = 0.0,
                  deterministic: bool = False) -> tuple[bool, str]:
        """The gate every signal passes through before it can be alerted."""
        if self.circuit_breaker_tripped:
            return False, (
                f"CIRCUIT BREAKER: down {self.weekly_drawdown_pct:.1f}% this week "
                f"(limit {self.config.drawdown_circuit_breaker_pct:.0f}%). "
                f"Stand down until next week."
            )
        if self.trades_today >= self.config.max_trades_per_day:
            return False, (
                f"Daily trade cap reached ({self.config.max_trades_per_day}). "
                f"More trades today means worse trades today."
            )
        if self.available_capital <= 0:
            return False, (
                f"Max concurrent exposure reached "
                f"(${self.config.max_concurrent_exposure:,.0f})."
            )
        if strategy and not self.config.strategy_enabled(strategy):
            gate = self.config.gate_for(strategy)
            return False, (
                f"'{strategy}' requires a ${gate:,.0f} bankroll "
                f"(currently ${self.config.bankroll:,.0f})."
            )
        floor = self.min_edge_for(deterministic)
        if edge and edge < floor:
            kind = "locked-arb" if deterministic else "forecast"
            return False, (
                f"Edge {edge * 100:.2f}% below the {kind} floor "
                f"{floor * 100:.2f}% for this bankroll."
            )
        return True, "ok"

    def record_trade(self, position: OpenPosition) -> None:
        self.roll_day()
        self.trades_today += 1
        self.open_positions.append(position)

    def status_line(self) -> str:
        state = "HALTED" if self.circuit_breaker_tripped else "OK"
        return (
            f"[{state}] bankroll ${self.current_bankroll:,.0f} | "
            f"exposure ${self.current_exposure:,.0f}/"
            f"${self.config.max_concurrent_exposure:,.0f} | "
            f"trades today {self.trades_today}/{self.config.max_trades_per_day} | "
            f"wk drawdown {self.weekly_drawdown_pct:.1f}%"
        )
