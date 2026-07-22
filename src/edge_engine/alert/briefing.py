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


def _header(window: BriefingWindow, state: DisciplineState,
            now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    config = state.config
    return [
        f"<b>{window.name}</b> · {now.strftime('%a %b %d')}",
        f"<code>Bankroll {money(config.bankroll)}   "
        f"Unit {money(config.unit_size())}</code>",
        f"<code>Trades {state.trades_today}/{config.max_trades_per_day}   "
        f"Exposure {money(state.current_exposure)}/"
        f"{money(config.max_concurrent_exposure)}</code>",
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

    lines = [
        f"<b>═══ {heading} ═══</b>",
        f"{tag} <b>{signal.title[:70]}</b>",
        f"<i>{signal.venue.value.title()} · {label} · "
        f"grade {grade(signal.score, signal.edge, signal.confidence, signal.deterministic)}</i>",
        "",
    ]

    if signal.deterministic:
        rationale = signal.rationale
        lines += [
            f"<code>BUY  {signal.side.replace('_', ' ').upper()}</code>",
            f"<code>Legs        {rationale.get('legs', '?')}</code>",
            f"<code>Cost        {money(rationale.get('cost_at_size'))}</code>",
            f"<code>Fees        {money(rationale.get('fees_at_size'))}</code>",
            f"<code>Guaranteed  {money(rationale.get('net_profit'))} "
            f"({signal.edge * 100:+.2f}%)</code>",
        ]
    else:
        lines += [
            f"<code>BET  {signal.side.upper()}</code>",
            f"<code>Market      {price_line(market)}</code>",
        ]
        if not signal.advisory and fair and fair != market:
            lines += [
                f"<code>Fair value  {price_line(fair)}</code>",
                f"<code>Your edge   {edge_points(fair, market):+.1f} pts</code>",
            ]
        else:
            lines.append(f"<code>Edge est.   {signal.edge * 100:+.2f}%</code>")

    lines.append(
        f"<code>Confidence  {confidence_bar(signal.confidence)} "
        f"{signal.confidence * 100:.0f}%</code>"
    )

    if signal.advisory:
        lines += [
            "",
            "<b>NO STAKE — research only.</b>",
            "<i>Screened wallets are positioned here. That is a reason to look, "
            "not a reason to bet. Form your own view first.</i>",
        ]
    elif signal.stake:
        win = payout(signal.stake, market)
        lines += [
            "",
            f"<b>STAKE {money(signal.stake)}</b>  "
            f"({signal.contracts:,.0f} contracts)",
            f"<code>Risk {money(signal.stake)} to win {money(win)}</code>",
        ]

    lines.append(
        f"<code>Resolves    {horizon(signal.days_to_resolution)} "
        f"({resolve_date(signal.days_to_resolution)})</code>"
    )

    why = _why(signal)
    if why:
        lines += ["", f"<b>Why:</b> {why}"]
    if signal.counter_case:
        lines += [f"<b>Against:</b> <i>{signal.counter_case[:300]}</i>"]

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
                   now: Optional[datetime] = None) -> str:
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
                "<i>One play clearly ahead of the field today. "
                "Concentration beats dilution when the gap is this wide.</i>",
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
                f"<code>Total at risk today {money(total)} "
                f"({total / max(state.config.bankroll, 1) * 100:.1f}% of bankroll)"
                f"</code>",
                "",
            ]

    if beyond and eligible:
        out += [
            f"<i>{len(beyond)} further opportunit"
            f"{'y' if len(beyond) == 1 else 'ies'} resolve beyond this window "
            f"— see {window.next_view}.</i>",
            "",
        ]

    if calibration_verdict:
        out += ["<b>─── CALIBRATION ───</b>", f"<i>{calibration_verdict}</i>", ""]

    out += [
        "<code>/took 1        logged as taken</code>",
        "<code>/skip 1        logged as passed</code>",
        "<code>/explain 1     full reasoning</code>",
        "",
        "<i>Order tickets, not advice. Verify every price before you click — "
        "nothing here is placed for you.</i>",
    ]
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
