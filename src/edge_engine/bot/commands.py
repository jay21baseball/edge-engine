"""Telegram command handlers.

Every handler takes (engine, chat_id, args) and returns text to send back.
Handlers may also return None and send progress messages themselves via the
`reply` callback, which matters for commands that take 30+ seconds.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Callable, Optional

from ..alert.briefing import DAILY, WEEKLY, build_briefing, render_play
from ..alert.format import (
    american_str,
    cents,
    edge_points,
    horizon,
    money,
    payout,
    price_line,
)
from ..journal.calibration import build_report
from ..strategies.base import Signal

log = logging.getLogger(__name__)

FRESH_SCAN_MAX_AGE_MINUTES = 20.0


def _signal_from_row(row: dict) -> Signal:
    from ..ingest.models import Venue
    return Signal(
        strategy=row["strategy"], venue=Venue(row["venue"]),
        market_id=row["market_id"], title=row["title"], side=row["side"],
        entry_price=row["entry_price"] or 0.0,
        est_probability=row["est_probability"] or 0.0,
        edge=row["edge"] or 0.0, confidence=row["confidence"] or 0.0,
        days_to_resolution=row["days_to_resolution"] or 1.0,
        category=row.get("category") or "",
        stake=row.get("stake"), contracts=row.get("contracts"),
        deterministic=bool(row.get("deterministic")),
        advisory=(row["strategy"] == "wallet_attention"),
        rationale=json.loads(row.get("rationale") or "{}"),
        counter_case=row.get("counter_case") or "",
    )


# ------------------------------------------------------------------ bankroll

def cmd_bankroll(engine, chat_id: str, args: list[str], reply=None) -> str:
    if args:
        raw = args[0].replace("$", "").replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            return "That did not look like a number. Try: <code>/bankroll 2500</code>"
        if amount < 50:
            return ("Below $50 there is nothing sensible to size. Fees and "
                    "minimum order sizes would eat any edge.")
        engine.set_bankroll(chat_id, amount)
        config = engine.bankroll
        return "\n".join([
            f"<b>Bankroll set: {money(amount)}</b>", "",
            "<code>Unit size        " + money(config.unit_size()) + "</code>",
            f"<code>Max per trade    {config.max_single_position_pct:.0f}% "
            f"({money(amount * config.max_single_position_pct / 100)})</code>",
            f"<code>Max exposure     {config.max_concurrent_exposure_pct:.0f}% "
            f"({money(config.max_concurrent_exposure)})</code>",
            f"<code>Trades per day   {config.max_trades_per_day}</code>",
            f"<code>Edge floor       "
            f"{config.effective_min_edge * 100:.2f}%</code>",
            f"<code>Kelly            {config.kelly_fraction:.0%} "
            f"(quarter)</code>",
            "",
            _tier_note(amount),
            "", "Now try <code>/dailyedge</code>",
        ])
    current = engine.bankroll.bankroll
    return (f"Bankroll is {money(current)}.\n"
            f"Change it with <code>/bankroll 5000</code>")


def _tier_note(amount: float) -> str:
    if amount < 15000:
        return ("<i>Cross-venue arbitrage stays locked until $15,000. Below "
                "that, one mis-filled leg costs 5-10% of your stack and wipes "
                "out roughly twenty successful arbs.</i>")
    if amount < 50000:
        return ("<i>Cross-venue arbitrage is now unlocked. Passive liquidity "
                "provision unlocks at $50,000.</i>")
    return "<i>All strategies unlocked.</i>"


# ------------------------------------------------------------------ briefing

def _run_briefing(engine, chat_id: str, window, reply) -> str:
    if not engine.has_bankroll(chat_id):
        engine.store.set_state(chat_id, "pending", window.name)
        return ("<b>What is your bankroll right now?</b>\n\n"
                "Reply with just the number, e.g. <code>2500</code>\n\n"
                "<i>Everything derives from it — unit size, edge floor, trade "
                "caps, and which strategies are even allowed.</i>")

    age = engine.minutes_since_scan()
    if age is None or age > FRESH_SCAN_MAX_AGE_MINUTES:
        if reply:
            reply("Scanning both venues… about 30 seconds.")
        engine.scan_once()

    signals = [_signal_from_row(r) for r in engine.store.recent_signals(limit=40)]
    verdict = build_report(engine.store.resolved_predictions()).verdict()
    text = build_briefing(signals, engine.state, window, verdict)
    engine.remember_briefing(chat_id, engine.store.recent_signals(limit=40),
                             window)
    return text


def cmd_daily(engine, chat_id, args, reply=None) -> str:
    return _run_briefing(engine, chat_id, DAILY, reply)


def cmd_weekly(engine, chat_id, args, reply=None) -> str:
    return _run_briefing(engine, chat_id, WEEKLY, reply)


def cmd_all(engine, chat_id, args, reply=None) -> str:
    from ..alert.briefing import BriefingWindow
    return _run_briefing(engine, chat_id,
                         BriefingWindow("ALL OPEN EDGE", 3650.0, 12), reply)


# ------------------------------------------------------------------- journal

def _resolve_index(engine, chat_id: str, args: list[str]) -> tuple:
    if not args:
        return None, "Which one? e.g. <code>/explain 1</code>"
    try:
        index = int(args[0])
    except ValueError:
        return None, "Give the play number, e.g. <code>/took 1</code>"
    ids = engine.store.get_state(chat_id, "briefing_ids", []) or []
    if not (1 <= index <= len(ids)):
        return None, (f"No play {index} in your last briefing "
                      f"({len(ids)} shown). Run <code>/dailyedge</code> first.")
    return ids[index - 1], None


def cmd_explain(engine, chat_id, args, reply=None) -> str:
    signal_id, error = _resolve_index(engine, chat_id, args)
    if error:
        return error
    row = engine.store.signal_by_id(signal_id)
    if not row:
        return "That signal is no longer on file."
    signal = _signal_from_row(row)
    lines = render_play(signal, int(args[0]))

    r = signal.rationale
    lines += ["<b>─── FULL REASONING ───</b>"]
    if signal.strategy == "wallet_attention":
        for w in (r.get("wallets") or []):
            lines.append(
                f"<code>{str(w.get('user'))[:16]:<17}entry "
                f"{cents(w.get('entry', 0))}  edge {w.get('entry_adj_edge'):+.3f}"
                f"  n={w.get('n_resolved')}  vol:pnl={w.get('vol_pnl')}</code>"
            )
        lines += [
            "",
            "<i>These wallets passed every screen: 30+ resolved positions, "
            "positive edge measured against their ENTRY price (not a win rate), "
            "no single trade dominating their P&L, and no market-maker "
            "signature. Roughly half the public leaderboard fails that last "
            "test alone.</i>",
        ]
    else:
        for key, value in r.items():
            if isinstance(value, (int, float, str, bool)):
                lines.append(f"<code>{str(key)[:22]:<23}{value}</code>")

    lines += ["", f"<code>/took {args[0]}</code>   or   "
                  f"<code>/skip {args[0]}</code>"]
    return "\n".join(lines)


def cmd_took(engine, chat_id, args, reply=None) -> str:
    signal_id, error = _resolve_index(engine, chat_id, args)
    if error:
        return error
    row = engine.store.signal_by_id(signal_id)
    entry = None
    if len(args) > 1:
        try:
            entry = float(args[1].replace("¢", "").replace("$", ""))
            if entry > 1:
                entry /= 100.0
        except ValueError:
            pass
    engine.store.record_decision(signal_id, True, actual_entry=entry,
                                 stake=row.get("stake"))
    engine.state.trades_today += 1

    quoted = row.get("entry_price") or 0
    lines = [f"<b>Logged as taken.</b> {row.get('title', '')[:60]}"]
    if entry:
        slip = (entry - quoted) * 100
        lines.append(f"<code>Filled {cents(entry)} vs quoted {cents(quoted)} "
                     f"({slip:+.1f} pts)</code>")
        if slip > 2:
            lines.append("<i>That is meaningful slippage. If it keeps "
                         "happening, the edge is decaying before you can act "
                         "and the alert threshold should rise.</i>")
    lines += [
        f"<code>Trades today {engine.state.trades_today}/"
        f"{engine.bankroll.max_trades_per_day}</code>",
        "",
        f"When it settles: <code>/result {args[0]} win</code> or "
        f"<code>/result {args[0]} loss</code>",
    ]
    return "\n".join(lines)


def cmd_skip(engine, chat_id, args, reply=None) -> str:
    signal_id, error = _resolve_index(engine, chat_id, args)
    if error:
        return error
    engine.store.record_decision(signal_id, False)
    return ("<b>Logged as passed.</b>\n\n"
            "<i>Recording passes matters as much as recording takes — it is "
            "the only way to tell whether the model is wrong or whether it is "
            "fine and simply not being executed. Opposite problems, opposite "
            "fixes.</i>")


def cmd_result(engine, chat_id, args, reply=None) -> str:
    signal_id, error = _resolve_index(engine, chat_id, args)
    if error:
        return error
    if len(args) < 2:
        return "Win or loss? e.g. <code>/result 1 win</code>"
    verdict = args[1].lower()
    if verdict not in ("win", "loss", "won", "lost", "w", "l"):
        return "Say <code>win</code> or <code>loss</code>."
    outcome = 1.0 if verdict.startswith("w") else 0.0

    row = engine.store.signal_by_id(signal_id)
    stake = row.get("stake") or 0
    price = row.get("actual_entry") or row.get("entry_price") or 0.5
    pnl = payout(stake, price) if outcome else -stake
    engine.store.resolve_signal(signal_id, outcome, pnl)
    engine.state.current_bankroll += pnl

    report = build_report(engine.store.resolved_predictions())
    return "\n".join([
        f"<b>Recorded: {'WIN' if outcome else 'LOSS'}</b>  {money(pnl)}",
        f"<code>Bankroll {money(engine.state.current_bankroll)}</code>",
        "",
        f"<i>{report.verdict()}</i>",
    ])


def cmd_scorecard(engine, chat_id, args, reply=None) -> str:
    card = engine.store.scorecard()
    report = build_report(engine.store.resolved_predictions())
    alerted = card.get("alerted") or 0
    taken = card.get("taken") or 0
    lines = [
        "<b>SCORECARD</b>", "",
        f"<code>Alerted    {alerted}</code>",
        f"<code>Taken      {taken}</code>",
        f"<code>Passed     {card.get('passed') or 0}</code>",
        f"<code>Resolved   {card.get('resolved') or 0}</code>",
        f"<code>Net P&L    {money(card.get('pnl') or 0)}</code>",
        "",
        "<b>─── CALIBRATION ───</b>",
        f"<i>{report.verdict()}</i>",
    ]
    if report.n:
        lines += ["", f"<pre>{report.reliability_table()}</pre>"]
    if alerted and taken / max(alerted, 1) < 0.15 and alerted >= 10:
        lines += ["", "<i>You are acting on under 15% of alerts. Either the "
                       "threshold is too loose, or the edge is decaying before "
                       "you can act. Both are fixable, but they need opposite "
                       "fixes — /explain a few you passed on.</i>"]
    return "\n".join(lines)


# -------------------------------------------------------------------- status

def cmd_status(engine, chat_id, args, reply=None) -> str:
    state, config = engine.state, engine.bankroll
    gates = "\n".join(
        f"<code>{'ON ' if config.strategy_enabled(name) else 'OFF'} "
        f"{name:<22}</code>"
        for name in config.strategy_gates
    )
    breaker = ("🛑 TRIPPED" if state.circuit_breaker_tripped else "✅ clear")
    return "\n".join([
        "<b>STATUS</b>", "",
        f"<code>Bankroll     {money(config.bankroll)}</code>",
        f"<code>Unit         {money(config.unit_size())}</code>",
        f"<code>Exposure     {money(state.current_exposure)} / "
        f"{money(config.max_concurrent_exposure)}</code>",
        f"<code>Trades today {state.trades_today}/"
        f"{config.max_trades_per_day}</code>",
        f"<code>Wk drawdown  {state.weekly_drawdown_pct:.1f}%</code>",
        f"<code>Breaker      {breaker}</code>",
        "", "<b>Strategies</b>", gates,
    ])


def cmd_wallets(engine, chat_id, args, reply=None) -> str:
    addresses = engine.store.qualified_wallets()
    if not addresses:
        return ("No qualified wallets cached. The rescore runs every few "
                "hours; nothing qualifying is itself a valid result.")
    lines = ["<b>QUALIFIED WALLETS</b>",
             "<i>Passed all screens, including market-maker exclusion.</i>", ""]
    rows = []
    for address in addresses[:15]:
        score = engine.store.latest_wallet_score(address)
        if score:
            rows.append((score.get("entry_adj_edge") or 0, score, address))
    for edge, score, address in sorted(rows, key=lambda r: -r[0]):
        lines.append(
            f"<code>{(score.get('username') or address[:10])[:16]:<17}"
            f"edge {edge:+.3f}  t={score.get('t_stat') or 0:.1f}  "
            f"n={score.get('n_resolved')}</code>"
        )
    lines += ["", "<i>Edge is measured against the price they PAID, not a win "
                  "rate. Buying 90c favourites and winning 90% of the time is "
                  "zero skill — the public leaderboard cannot see that.</i>"]
    return "\n".join(lines)


def cmd_help(engine, chat_id, args, reply=None) -> str:
    return "\n".join([
        "<b>FORGE — prediction market edge</b>", "",
        "<b>Daily use</b>",
        "<code>/dailyedge</code>    best plays to enter today",
        "<code>/weeklyedge</code>   the week's plan",
        "<code>/all</code>          every open opportunity", "",
        "<b>Acting on a play</b>",
        "<code>/explain 1</code>    full reasoning and the counter-case",
        "<code>/took 1 46</code>    logged as taken at 46¢",
        "<code>/skip 1</code>       logged as passed",
        "<code>/result 1 win</code> settle it", "",
        "<b>Tracking</b>",
        "<code>/scorecard</code>    P&L and whether the model is calibrated",
        "<code>/status</code>       exposure, caps, circuit breaker",
        "<code>/wallets</code>      the screened traders being followed",
        "<code>/bankroll 2500</code> resize everything", "",
        "<i>Nothing is ever traded for you. Every play is an order ticket you "
        "fill yourself.</i>",
    ])


HANDLERS: dict[str, Callable] = {
    "start": cmd_help, "help": cmd_help,
    "bankroll": cmd_bankroll,
    "dailyedge": cmd_daily, "daily": cmd_daily,
    "weeklyedge": cmd_weekly, "weekly": cmd_weekly,
    "all": cmd_all,
    "explain": cmd_explain, "why": cmd_explain,
    "took": cmd_took, "take": cmd_took,
    "skip": cmd_skip, "pass": cmd_skip,
    "result": cmd_result, "settle": cmd_result,
    "scorecard": cmd_scorecard, "score": cmd_scorecard,
    "status": cmd_status,
    "wallets": cmd_wallets,
}

# Natural phrasings, so the bot answers "daily edge" as readily as "/dailyedge".
ALIASES = {
    "daily edge": "dailyedge", "todays edge": "dailyedge",
    "today's edge": "dailyedge", "whats today": "dailyedge",
    "weekly edge": "weeklyedge", "this week": "weeklyedge",
    "score card": "scorecard", "my score": "scorecard",
    "what do i do": "dailyedge", "any plays": "dailyedge",
}
