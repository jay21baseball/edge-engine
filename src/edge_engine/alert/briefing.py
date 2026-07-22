"""Daily and weekly edge briefings in betting format.

Design rule: quality over coverage. If one play is clearly the best available,
say so and show only it. A list of seven mediocre options is how a small bankroll
gets spread thin across trades that individually did not deserve funding.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ..sizing.bankroll import DisciplineState
from ..strategies.base import Signal
from .format import (
    american_str,
    cents,
    confidence_bar,
    edge_points,
    grade,
    horizon,
    money,
    payout,
    price_line,
    resolve_date,
)

# A play must beat the runner-up by this much to be presented alone.
CONVICTION_RATIO = 2.0
DAILY_MAX_PLAYS = 3
WEEKLY_MAX_PLAYS = 7

STRATEGY_LABEL = {
    "combinatorial_arb": "Locked arbitrage",
    "wallet_attention": "Sharp money",
    "sportsbook_divergence": "Stale line vs sharp book",
    "weather": "Forecast model",
    "favorite_longshot": "Structural bias",
}


@dataclass
class BriefingWindow:
    name: str
    max_days: float
    max_plays: int
    empty_headline: str = "NO PLAY TODAY"
    next_view: str = "/weeklyedge or /all"


DAILY = BriefingWindow("DAILY EDGE", max_days=3.0, max_plays=DAILY_MAX_PLAYS,
                       empty_headline="NO PLAY TODAY",
                       next_view="/weeklyedge or /all")
WEEKLY = BriefingWindow("WEEKLY EDGE", max_days=10.0, max_plays=WEEKLY_MAX_PLAYS,
                        empty_headline="NO PLAYS THIS WEEK",
                        next_view="/all")


RULE = "━" * 22
THIN = "─" * 22


def _header(window: BriefingWindow, state: DisciplineState,
            now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    config = state.config
    lines = [
        f"<b>{window.name}</b>",
        f"<i>{now.strftime('%A, %B %d')}</i>",
        RULE,
        f"<code>BANKROLL   {money(config.bankroll)}</code>",
        f"<code>UNIT       {money(config.unit_size())}</code>",
        f"<code>TRADES     {state.trades_today} of "
        f"{config.max_trades_per_day}</code>",
        f"<code>EXPOSURE   {money(state.current_exposure)} of "
        f"{money(config.max_concurrent_exposure)}</code>",
        "",
    ]
    if config.is_paper_mode:
        lines += _paper_mode_notice(config)
    return lines


def _paper_mode_notice(config) -> list[str]:
    """Say plainly what a sub-minimum unit means, in dollars.

    Percentages hide this: a 5% edge sounds healthy until it is priced out as
    nineteen cents against a fee that is structurally ~4% of stake.
    """
    per_trade = config.expected_profit_per_trade(0.05)
    return [
        "📋 <b>PAPER MODE</b>",
        f"<i>A unit of {money(config.unit_size())} earns "
        f"{money(per_trade)} on a strong 5% edge, and Polymarket rejects "
        f"orders under 5 shares. Track the calls and log outcomes to build "
        f"the record — the analysis is identical, only the stakes are not "
        f"real yet. Live sizing starts around "
        f"{money(config.bankroll_for_live)}.</i>",
        "",
    ]


def _stand_down(state: DisciplineState) -> list[str]:
    return [
        "🛑 <b>STAND DOWN</b>",
        f"Down {state.weekly_drawdown_pct:.1f}% this week "
        f"(limit {state.config.drawdown_circuit_breaker_pct:.0f}%).",
        "",
        "<i>No trades until the week resets. This rule only ever fires when "
        "you least want it to — that is precisely when it earns its keep.</i>",
    ]


def render_play(signal: Signal, index: Optional[int], solo: bool = False) -> list[str]:
    """One play as a betting card."""
    fair = signal.est_probability
    market = signal.entry_price
    label = STRATEGY_LABEL.get(signal.strategy, signal.strategy)
    tag = "🔒" if signal.deterministic else ("👁" if signal.advisory else "📈")
    heading = "THE PLAY" if solo else f"PLAY {index}"

    mark = grade(signal.score, signal.edge, signal.confidence,
                 signal.deterministic)
    lines = [
        RULE,
        f"<b>{heading}</b>  ·  grade <b>{mark}</b>",
        f"{tag} <b>{signal.title[:70]}</b>",
        f"<i>{signal.venue.value.title()} · {label}</i>",
        "",
    ]

    if signal.deterministic:
        r = signal.rationale
        lines += [
            f"<code>ACTION     BUY {signal.side.replace('_', ' ').upper()}</code>",
            f"<code>LEGS       {r.get('legs', '?')}</code>",
            f"<code>COST       {money(r.get('cost_at_size'))}</code>",
            f"<code>FEES       {money(r.get('fees_at_size'))}</code>",
            f"<code>LOCKED     {money(r.get('net_profit'))} "
            f"({signal.edge * 100:+.2f}%)</code>",
        ]
    else:
        lines += [
            f"<code>ACTION     BET {signal.side.upper()}</code>",
            f"<code>MARKET     {price_line(market)}</code>",
        ]
        if not signal.advisory and fair and fair != market:
            lines += [
                f"<code>FAIR       {price_line(fair)}</code>",
                f"<code>EDGE       {edge_points(fair, market):+.1f} pts</code>",
            ]
        else:
            lines.append(f"<code>EDGE       {signal.edge * 100:+.2f}% est.</code>")

    lines += [
        f"<code>CONFIDENCE {confidence_bar(signal.confidence)} "
        f"{signal.confidence * 100:.0f}%</code>",
        f"<code>RESOLVES   {horizon(signal.days_to_resolution)} · "
        f"{resolve_date(signal.days_to_resolution)}</code>",
    ]

    if signal.advisory:
        lines += [
            "",
            "<b>NO STAKE — research only</b>",
            "<i>Screened wallets are positioned here. That is a reason to look, "
            "not a reason to bet.</i>",
        ]
    elif signal.stake is None:
        # Paper mode. The analysis stands; the stake is withheld deliberately.
        lines += [
            "",
            "<b>NO STAKE — paper mode</b>",
            "<i>Log it with /took or /skip to build the record. Stakes return "
            "once the bankroll can support a real position.</i>",
        ]
    elif signal.stake:
        win = payout(signal.stake, market)
        lines += [
            "",
            f"<code>STAKE      {money(signal.stake)} "
            f"({signal.contracts:,.0f} contracts)</code>",
            f"<code>TO WIN     {money(win)}</code>",
        ]

    why = _why(signal)
    if why:
        lines += ["", f"<b>WHY</b>  {why}"]
    if signal.counter_case:
        lines += ["", f"<b>AGAINST</b>  <i>{signal.counter_case[:300]}</i>"]

    lines.append("")
    return lines


def _why(signal: Signal) -> str:
    r = signal.rationale
    if signal.strategy == "wallet_attention":
        wallets = r.get("wallets") or []
        who = ", ".join(w.get("user", "?")[:14] for w in wallets[:3])
        captured = r.get("move_already_captured_pct", 0)
        moved = ("price has moved against them — you can enter better than "
                 "they did" if captured == 0 else
                 f"{captured}% of their move is already gone")
        return (f"{r.get('agreeing_wallets')} screened wallets in at "
                f"{cents(r.get('their_avg_entry', 0))} ({who}); {moved}.")
    if signal.strategy == "sportsbook_divergence":
        return (f"{r.get('sharp_source')} prices this at "
                f"{american_str(r.get('devigged_probability', 0.5))}, "
                f"Polymarket is offering "
                f"{american_str(r.get('polymarket_ask', 0.5))} — "
                f"{r.get('raw_gap_points')} points of stale line.")
    if signal.strategy == "combinatorial_arb":
        enforced = ("payout enforced on-chain by negRisk"
                    if r.get("neg_risk_enforced") else
                    "verify the outcome set is genuinely exhaustive")
        return (f"Every outcome bought below the guaranteed payout; "
                f"{enforced}.")
    return ""


def build_briefing(signals: list[Signal], state: DisciplineState,
                   window: BriefingWindow = DAILY,
                   calibration_verdict: str = "",
                   now: Optional[datetime] = None,
                   calibration_detail: str = "") -> str:
    """Render a briefing. Ranked, filtered, and deliberately short."""
    out = _header(window, state, now)

    if state.circuit_breaker_tripped:
        return "\n".join(out + _stand_down(state))

    eligible = [s for s in signals if s.days_to_resolution <= window.max_days]
    # Anything resolving beyond the window is still worth naming, but not
    # worth funding today - it would lock capital past the horizon being planned.
    beyond = [s for s in signals if s.days_to_resolution > window.max_days]
    eligible.sort(key=lambda s: -s.score)

    if not eligible:
        out += _nothing(beyond, window)
    else:
        top = eligible[: window.max_plays]
        runner_up = top[1].score if len(top) > 1 else 0.0
        solo = (len(top) > 1 and runner_up > 0
                and top[0].score / runner_up >= CONVICTION_RATIO)

        if solo:
            out += [
                "<i>One play clearly ahead of the field. Concentration beats "
                "dilution when the gap is this wide.</i>",
                "",
            ]
            out += render_play(top[0], 1, solo=True)
        else:
            for i, signal in enumerate(top, 1):
                out += render_play(signal, i)

        actionable = [s for s in top if not s.advisory]
        if actionable:
            total = sum(s.stake or 0 for s in actionable)
            out += [
                THIN,
                f"<code>AT RISK    {money(total)} "
                f"({total / max(state.config.bankroll, 1) * 100:.1f}% of "
                f"bankroll)</code>",
                "",
            ]

    if beyond and eligible:
        out += [
            f"<i>{len(beyond)} further opportunit"
            f"{'y' if len(beyond) == 1 else 'ies'} resolve beyond this window "
            f"— see {window.next_view}.</i>",
            "",
        ]

    # Track record lives in /scorecard, not here. It is the number that decides
    # whether any of this is real, but repeating it on every briefing turned it
    # into wallpaper — and a metric you have stopped reading is worse than one
    # you have to go and look at.
    _ = (calibration_verdict, calibration_detail)

    if eligible:
        out += [THIN,
                "<code>/explain 1</code> · <code>/took 1</code> · "
                "<code>/skip 1</code>"]
    return "\n".join(out)


def _nothing(beyond: list[Signal], window: BriefingWindow) -> list[str]:
    lines = [f"<b>{window.empty_headline}</b>", ""]
    if beyond:
        soonest = min(s.days_to_resolution for s in beyond)
        lines.append(
            f"<i>{len(beyond)} opportunit"
            f"{'y' if len(beyond) == 1 else 'ies'} exist but the nearest "
            f"resolves in {horizon(soonest)}, past this window. At a small "
            f"bankroll, capital locked that long has to clear a much higher "
            f"bar — see {window.next_view}.</i>"
        )
    else:
        lines.append(
            "<i>Nothing cleared the threshold. This is the normal outcome and "
            "the correct one — a system that finds a trade every single day is "
            "not finding edge, it is lowering its standards.</i>"
        )
    lines.append("")
    return lines
