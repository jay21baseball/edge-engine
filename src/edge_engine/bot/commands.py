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

from ..alert.briefing import (
    DAILY,
    STRATEGY_LABEL,
    WEEKLY,
    build_briefing,
    render_play,
)
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

    rows = engine.store.recent_signals(limit=40)
    signals = [_signal_from_row(r) for r in rows]
    report = build_report(engine.store.resolved_predictions())
    text = build_briefing(signals, engine.state, window, report.verdict(),
                          calibration_detail=report.detail())
    engine.remember_briefing(chat_id, rows, window)
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
        "<b>SCORECARD</b>", "─" * 22,
        f"<code>ALERTED    {alerted}</code>",
        f"<code>TAKEN      {taken}</code>",
        f"<code>PASSED     {card.get('passed') or 0}</code>",
        f"<code>RESOLVED   {card.get('resolved') or 0}</code>",
        f"<code>NET P&L    {money(card.get('pnl') or 0)}</code>",
        "",
        "<b>TRACK RECORD</b>",
        f"<code>{report.verdict()}</code>",
        f"<i>{report.detail()}</i>",
    ]
    if report.n:
        lines += ["", f"<pre>{report.reliability_table()}</pre>"]
    if alerted and taken / max(alerted, 1) < 0.15 and alerted >= 10:
        lines += ["", "<i>You are acting on under 15% of alerts. Either the "
                       "threshold is too loose, or the edge is decaying before "
                       "you can act. Both are fixable, but they need opposite "
                       "fixes — /explain a few you passed on.</i>"]
    return "\n".join(lines)


def cmd_whynot(engine, chat_id, args, reply=None) -> str:
    """The strongest opportunities that were REJECTED, and why.

    The most useful diagnostic here. If the near-misses look like money, the
    thresholds are too tight; if they look like junk, they are right. Seeing
    only what passed tells you nothing about what a different threshold would
    have caught.
    """
    rejected = engine.store.get_state("_system", "last_rejections", []) or []
    if not rejected:
        return ("Nothing was rejected on the last scan — every candidate "
                "either passed or never reached the discipline layer.\n\n"
                "<i>Run <code>/dailyedge</code> first if you have not scanned "
                "today.</i>")
    lines = ["<b>REJECTED · NEAR MISSES</b>",
             "<i>What the filters turned down, strongest first.</i>", ""]
    for item in rejected[:6]:
        lines += [
            f"<b>{item['title']}</b>",
            f"<code>EDGE       {item['edge'] * 100:+.2f}% at "
            f"{american_str(item['price'])}</code>",
            f"<code>RESOLVES   {horizon(item['days'])}</code>",
            f"<code>REJECTED   {item['reason'][:60]}</code>",
            "",
        ]
    lines.append(
        "<i>If these keep looking like money left on the table, the edge floor "
        "is too high. If they look like junk, the filters are working. That "
        "judgement is yours — the system cannot make it for you.</i>"
    )
    return "\n".join(lines)


def cmd_pnl(engine, chat_id, args, reply=None) -> str:
    """P&L by strategy, so you learn which edge is actually paying."""
    days = 7.0
    if args:
        days = {"day": 1.0, "today": 1.0, "week": 7.0, "month": 30.0,
                "all": 3650.0}.get(args[0].lower(), 7.0)
    rows = engine.store.pnl_by_strategy(days)
    if not rows:
        return f"No signals recorded in the last {days:.0f} days."

    label = {1.0: "TODAY", 7.0: "THIS WEEK",
             30.0: "THIS MONTH"}.get(days, "ALL TIME")
    lines = [f"<b>P&L BY STRATEGY · {label}</b>", ""]
    total = 0.0
    for row in rows:
        name = STRATEGY_LABEL.get(row["strategy"], row["strategy"])
        wins, losses = row.get("wins") or 0, row.get("losses") or 0
        settled = wins + losses
        pnl = row.get("pnl") or 0.0
        total += pnl
        lines += [f"<b>{name}</b>",
                  f"<code>ALERTED    {row.get('alerted') or 0}</code>",
                  f"<code>TAKEN      {row.get('taken') or 0}</code>"]
        if settled:
            lines.append(f"<code>RECORD     {wins}-{losses} "
                         f"({wins / settled * 100:.0f}%)</code>")
        lines += [f"<code>P&L        {money(pnl)}</code>", ""]
    lines += ["─" * 22, f"<code>TOTAL      {money(total)}</code>", ""]

    settled_any = sum((r.get("wins") or 0) + (r.get("losses") or 0)
                      for r in rows)
    if settled_any < 20:
        lines.append(
            f"<i>Only {settled_any} settled result"
            f"{'' if settled_any == 1 else 's'} so far. Any strategy can look "
            f"brilliant or broken over a handful of trades — this table means "
            f"little until roughly 20 each.</i>"
        )
    return "\n".join(lines)


def cmd_watch(engine, chat_id, args, reply=None) -> str:
    """Track a market and alert when it crosses a price you name."""
    watchlist = engine.store.get_state(chat_id, "watchlist", []) or []

    if not args:
        if not watchlist:
            return ("<b>WATCHLIST · empty</b>\n\n"
                    "<code>/watch cuba 40</code>\n\n"
                    "<i>Alerts when a market matching “cuba” trades at 40¢ or "
                    "better.</i>")
        lines = ["<b>WATCHLIST</b>", ""]
        for i, item in enumerate(watchlist, 1):
            lines.append(f"<code>{i}. {item['term'][:22]:<23}"
                         f"{american_str(item['target'])} "
                         f"({cents(item['target'])})</code>")
        lines += ["", "<code>/watch clear</code> to reset"]
        return "\n".join(lines)

    if args[0].lower() in ("clear", "reset", "none"):
        engine.store.set_state(chat_id, "watchlist", [])
        return "Watchlist cleared."
    if len(args) < 2:
        return ("Give a search term and a target price:\n"
                "<code>/watch cuba 40</code>")
    try:
        target = float(args[-1].replace("¢", "").replace("$", ""))
        if target > 1:
            target /= 100.0
    except ValueError:
        return "The last value must be a price, e.g. <code>40</code> for 40¢."
    if not (0 < target < 1):
        return "Target must be between 1¢ and 99¢."

    term = " ".join(args[:-1]).lower()
    watchlist = [w for w in watchlist if w["term"] != term]
    watchlist.append({"term": term, "target": target})
    engine.store.set_state(chat_id, "watchlist", watchlist[:20])
    return "\n".join([
        f"<b>Watching “{term}”</b>",
        f"<code>ALERT AT   {american_str(target)} ({cents(target)}) "
        f"or better</code>", "",
        f"<i>{len(watchlist)} on watch · checked every scan</i>",
    ])


def check_watchlist(engine, chat_id: str, events) -> list[str]:
    """Alert texts for any watched market that hit its target.

    Fired keys are remembered so a market sitting below your target does not
    re-alert on every scan for the rest of the week.
    """
    watchlist = engine.store.get_state(chat_id, "watchlist", []) or []
    if not watchlist:
        return []
    fired: list[str] = []
    already = set(engine.store.get_state(chat_id, "watch_fired", []) or [])
    for event in events:
        for market in event.markets:
            if market.status != "open" or not market.yes_ask:
                continue
            haystack = f"{event.title} {market.title}".lower()
            for item in watchlist:
                if item["term"] not in haystack:
                    continue
                if market.yes_ask > item["target"]:
                    continue
                key = f"{market.market_id}:{item['target']}"
                if key in already:
                    continue
                already.add(key)
                fired.append("\n".join([
                    "🔔 <b>WATCH TRIGGERED</b>",
                    f"<b>{market.title[:70]}</b>",
                    f"<code>NOW        {american_str(market.yes_ask)} "
                    f"({cents(market.yes_ask)})</code>",
                    f"<code>TARGET     {american_str(item['target'])} "
                    f"({cents(item['target'])})</code>", "",
                    "<i>Price alert only — no edge has been assessed. You "
                    "asked to be told, so you are being told.</i>",
                ]))
    engine.store.set_state(chat_id, "watch_fired", sorted(already)[-200:])
    return fired


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


HELP_TEXT = "\n".join([
    "<b>FORGE</b>",
    "<i>Prediction market edge · Kalshi + Polymarket</i>",
    "━" * 22, "",
    "<b>FIND PLAYS</b>",
    "<code>/dailyedge</code>   best plays to enter today",
    "<code>/weeklyedge</code>  the week's plan",
    "<code>/all</code>         every open opportunity",
    "<code>/whynot</code>      what got rejected, and why",
    "",
    "<b>ACT ON A PLAY</b>",
    "<code>/explain 1</code>   full reasoning + counter-case",
    "<code>/took 1 46</code>   logged as taken at 46¢",
    "<code>/skip 1</code>      logged as passed",
    "<code>/result 1 win</code>  settle it",
    "",
    "<b>TRACK</b>",
    "<code>/pnl week</code>    which strategy is actually paying",
    "<code>/scorecard</code>   record + calibration",
    "<code>/status</code>      exposure, caps, breaker",
    "<code>/wallets</code>     screened traders being followed",
    "",
    "<b>SET UP</b>",
    "<code>/bankroll 2500</code>  resize everything",
    "<code>/watch cuba 40</code>  alert when it hits 40¢",
    "<code>/help</code>        this list",
    "",
    "━" * 22,
    "<i>Plain English works too — “daily edge”, “any plays”.</i>",
    "<i>Nothing is ever traded for you.</i>",
])

# Registered with Telegram so typing "/" shows a menu of these.
COMMAND_MENU = [
    ("dailyedge", "Best plays to enter today"),
    ("weeklyedge", "The week's plan"),
    ("all", "Every open opportunity"),
    ("whynot", "What got rejected, and why"),
    ("explain", "Full reasoning for a play"),
    ("took", "Log a play as taken"),
    ("skip", "Log a play as passed"),
    ("result", "Settle a play win/loss"),
    ("pnl", "P&L by strategy"),
    ("scorecard", "Record and calibration"),
    ("status", "Exposure, caps, breaker"),
    ("wallets", "Screened traders followed"),
    ("watch", "Alert at a target price"),
    ("bankroll", "Set bankroll and resize"),
    ("help", "All commands"),
]


def cmd_help(engine, chat_id, args, reply=None) -> str:
    return HELP_TEXT


HANDLERS: dict[str, Callable] = {
    "start": cmd_help, "help": cmd_help, "commands": cmd_help,
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
    "whynot": cmd_whynot, "why_not": cmd_whynot, "rejected": cmd_whynot,
    "pnl": cmd_pnl, "profit": cmd_pnl,
    "watch": cmd_watch, "watchlist": cmd_watch,
}

# Natural phrasings, so the bot answers "daily edge" as readily as "/dailyedge".
ALIASES = {
    "daily edge": "dailyedge", "todays edge": "dailyedge",
    "today's edge": "dailyedge", "whats today": "dailyedge",
    "weekly edge": "weeklyedge", "this week": "weeklyedge",
    "score card": "scorecard", "my score": "scorecard",
    "what do i do": "dailyedge", "any plays": "dailyedge",
    "why not": "whynot", "what got rejected": "whynot",
    "near misses": "whynot", "rejected": "whynot",
    "profit": "pnl", "how am i doing": "pnl",
    "watch list": "watch", "my watchlist": "watch",
}
