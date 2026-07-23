"""Daily and weekly edge briefings, written like a friend who trades.

Two rules drive the voice here:

- Talk plainly. No box-drawing bars, no monospace label columns, no emoji used
  as structure, no em-dashes. Real numbers in real sentences. It should read
  like a text from someone sharp, not a machine report.
- Quality over coverage. If one play clearly beats the rest, show only it. A
  list of seven mediocre options is how a small bankroll gets spread across
  trades that individually did not deserve funding.

Everything that matters to profit still shows: what you pay, what you win, the
edge in points and American odds, when it settles, why, and the catch.
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
    edge_points,
    grade,
    horizon,
    money,
    payout,
    price_line,
    resolve_date,
)

CONVICTION_RATIO = 2.0
DAILY_MAX_PLAYS = 3
WEEKLY_MAX_PLAYS = 7

STRATEGY_LABEL = {
    "combinatorial_arb": "locked arbitrage",
    "wallet_attention": "sharp money read",
    "sportsbook_divergence": "stale line vs a sharp book",
    "weather": "forecast model",
    "favorite_longshot": "structural mispricing",
}


@dataclass
class BriefingWindow:
    name: str
    max_days: float
    max_plays: int
    empty_headline: str = "Nothing worth a bet today."
    next_view: str = "/weeklyedge or /all"


DAILY = BriefingWindow("today", max_days=3.0, max_plays=DAILY_MAX_PLAYS,
                       empty_headline="Nothing worth a bet today.",
                       next_view="/weeklyedge or /all")
WEEKLY = BriefingWindow("this week", max_days=10.0, max_plays=WEEKLY_MAX_PLAYS,
                        empty_headline="Nothing worth a bet this week.",
                        next_view="/all")


def _greeting(now: datetime) -> str:
    h = now.hour
    if h < 12:
        return "Morning."
    if h < 18:
        return "Afternoon."
    return "Evening."


def _header(window: BriefingWindow, state: DisciplineState,
            now: Optional[datetime] = None) -> list[str]:
    now = now or datetime.now(timezone.utc)
    config = state.config
    lines = [f"{_greeting(now)} Here's what I've got for {window.name}.", ""]
    if config.is_paper_mode:
        lines += _paper_mode_notice(config)
    else:
        risk = (f"nothing at risk yet" if state.current_exposure <= 0
                else f"{money(state.current_exposure)} at risk")
        lines += [
            f"You're working with {money(config.bankroll)}, unit size "
            f"{money(config.unit_size())}. {state.trades_today} trades in "
            f"today, {risk}.",
            "",
        ]
    return lines


def _paper_mode_notice(config) -> list[str]:
    per_trade = config.expected_profit_per_trade(0.05)
    return [
        f"You're on {money(config.bankroll)}, so we're in paper mode. I'll give "
        f"you the full read on everything, but I'm holding the bet sizes back "
        f"until real money makes sense.",
        f"Your unit would be {money(config.unit_size())}, so a strong 5% edge "
        f"is only about {money(per_trade)} a trade, and Polymarket won't even "
        f"take an order under 5 shares. Real sizing kicks in around "
        f"{money(config.bankroll_for_live)}.",
        "",
        "Still log everything with /took and /skip. That's how we find out if "
        "any of this actually works before a dollar is on the line.",
        "",
    ]


def _stand_down(state: DisciplineState) -> list[str]:
    return [
        f"Sit this week out. You're down {state.weekly_drawdown_pct:.1f}% since "
        f"the week started, past the {state.config.drawdown_circuit_breaker_pct:.0f}"
        f"% line I won't let you trade through.",
        "",
        "This always feels wrong in the moment, and that's exactly when it's "
        "doing its job. Back at it next week.",
    ]


def render_play(signal: Signal, index: Optional[int], solo: bool = False) -> list[str]:
    """One play, written plainly. Numbers that matter, said like a person."""
    market = signal.entry_price
    fair = signal.est_probability
    label = STRATEGY_LABEL.get(signal.strategy, signal.strategy)
    mark = grade(signal.score, signal.edge, signal.confidence,
                 signal.deterministic)
    head = "The one to look at" if solo else f"{index}."

    lines = [f"{head}  {signal.title[:75]}",
             f"({signal.venue.value.title()}, {label}, grade {mark})", ""]

    if signal.deterministic:
        r = signal.rationale
        lines.append(
            f"This one's locked in. Buy every outcome and one has to pay. "
            f"Costs about {money(r.get('cost_at_size'))} with "
            f"{money(r.get('fees_at_size'))} in fees, and you're guaranteed "
            f"{money(r.get('guaranteed_payout') or r.get('net_profit'))} back. "
            f"Clean {signal.edge * 100:.1f}%, no guessing."
        )
    else:
        act = signal.side.upper()
        line = (f"Bet {act}. It's trading at {price_line(market)}")
        if not signal.advisory and fair and fair != market:
            line += (f", but I've got fair value nearer {price_line(fair)} "
                     f"- about {edge_points(fair, market):+.0f} points your way.")
        else:
            line += f", roughly {signal.edge * 100:+.1f}% of edge on my read."
        lines.append(line)

    if signal.advisory:
        lines += ["",
                  "Not sizing this one. It's a research flag: screened wallets "
                  "are in here, which is a reason to look, not a reason to bet."]
    elif signal.stake is None and not signal.deterministic:
        lines += ["",
                  "Holding the size while you're in paper mode. Log it with "
                  "/took or /skip so it still counts toward the record."]
    elif signal.stake:
        win = payout(signal.stake, market)
        lines += ["",
                  f"Put down {money(signal.stake)} ({signal.contracts:,.0f} "
                  f"contracts) to win {money(win)}."]

    lines.append(f"Settles {horizon(signal.days_to_resolution)}, around "
                 f"{resolve_date(signal.days_to_resolution)}.")

    why = _why(signal)
    if why:
        lines += ["", f"Why: {why}"]
    if signal.counter_case:
        lines += [f"The catch: {signal.counter_case[:280]}"]

    lines.append("")
    return lines


def _why(signal: Signal) -> str:
    r = signal.rationale
    if signal.strategy == "wallet_attention":
        wallets = r.get("wallets") or []
        who = ", ".join(w.get("user", "?")[:14] for w in wallets[:3])
        captured = r.get("move_already_captured_pct", 0)
        moved = ("the price has actually drifted your way since they got in"
                 if captured == 0 else
                 f"{captured}% of their move is already gone")
        return (f"{r.get('agreeing_wallets')} screened wallets got in around "
                f"{cents(r.get('their_avg_entry', 0))} ({who}), and {moved}.")
    if signal.strategy == "sportsbook_divergence":
        return (f"the sharp book has this at "
                f"{american_str(r.get('devigged_probability', 0.5))} but "
                f"Polymarket is still offering "
                f"{american_str(r.get('polymarket_ask', 0.5))} - "
                f"{r.get('raw_gap_points')} points of stale line.")
    if signal.strategy == "combinatorial_arb":
        return ("every outcome is priced below the guaranteed payout"
                + (", enforced on-chain."
                   if r.get("neg_risk_enforced")
                   else " - just double-check the outcome set is complete."))
    return ""


def build_briefing(signals: list[Signal], state: DisciplineState,
                   window: BriefingWindow = DAILY,
                   calibration_verdict: str = "",
                   now: Optional[datetime] = None,
                   calibration_detail: str = "") -> str:
    out = _header(window, state, now)

    if state.circuit_breaker_tripped:
        return "\n".join(out + _stand_down(state))

    eligible = [s for s in signals if s.days_to_resolution <= window.max_days]
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
            out += ["One play stands out today, so I'm only showing you that "
                    "one. When the gap's this wide, concentration beats "
                    "spreading thin.", ""]
            out += render_play(top[0], 1, solo=True)
        else:
            for i, signal in enumerate(top, 1):
                out += render_play(signal, i)

        actionable = [s for s in top if not s.advisory and s.stake]
        if actionable:
            total = sum(s.stake or 0 for s in actionable)
            out += [f"All in, that's {money(total)} at risk today, about "
                    f"{total / max(state.config.bankroll, 1) * 100:.1f}% of your "
                    f"bankroll.", ""]

    if beyond and eligible:
        n = len(beyond)
        out += [f"There {'is' if n == 1 else 'are'} {n} more worth watching "
                f"that settle further out. Check {window.next_view}.", ""]

    _ = (calibration_verdict, calibration_detail)

    if eligible:
        out += ["Reply /took 1 if you take it, /skip 1 if you pass, or "
                "/explain 1 for the full read."]
    return "\n".join(out)


def _nothing(beyond: list[Signal], window: BriefingWindow) -> list[str]:
    lines = [window.empty_headline]
    if beyond:
        soonest = min(s.days_to_resolution for s in beyond)
        n = len(beyond)
        lines.append(
            f"There {'is' if n == 1 else 'are'} {n} out there, but the nearest "
            f"settles {horizon(soonest)} out, past this window. Money locked up "
            f"that long has a higher bar to clear at your size. Check "
            f"{window.next_view} if you want to see them.")
    else:
        lines.append(
            "That's normal, honestly. Most days are quiet, and a system that "
            "finds a play every single day isn't finding an edge, it's lowering "
            "the bar. I'll text you the second something real shows up.")
    lines.append("")
    return lines
