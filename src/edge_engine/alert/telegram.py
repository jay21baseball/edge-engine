"""Telegram delivery and the daily briefing.

Alerts are order tickets, not instructions. Every one carries its own counter-case
so the reason NOT to take it is as visible as the reason to take it.
"""
from __future__ import annotations

import logging
import urllib.parse
from typing import Optional

from ..ingest.http import request_json
from ..sizing.bankroll import DisciplineState
from ..strategies.base import Signal

log = logging.getLogger(__name__)

# Horizon sections, as requested: today / week / month / long-dated.
HORIZONS = [
    ("TODAY", 0.0, 1.5),
    ("THIS WEEK", 1.5, 8.0),
    ("THIS MONTH", 8.0, 35.0),
    ("LONG-DATED", 35.0, 10_000.0),
]


class TelegramNotifier:
    def __init__(self, bot_token: Optional[str], chat_id: Optional[str],
                 enabled: bool = True):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.enabled = enabled and bool(bot_token) and bool(chat_id)
        if not self.enabled:
            log.info("Telegram disabled - alerts will print to console only.")

    def send(self, text: str) -> bool:
        if not self.enabled:
            print(text)
            return False
        try:
            request_json(
                f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
                f"?chat_id={urllib.parse.quote(str(self.chat_id))}"
                f"&parse_mode=HTML&disable_web_page_preview=true"
                f"&text={urllib.parse.quote(text[:4000])}"
            )
            return True
        except Exception as e:
            log.error("Telegram send failed: %s", e)
            print(text)
            return False


def format_signal(signal: Signal, index: Optional[int] = None) -> str:
    prefix = f"<b>{index}. </b>" if index else ""
    tag = ("🔒 ARB" if signal.deterministic
           else "👁 RESEARCH" if signal.advisory else "📈 TRADE")
    lines = [
        f"{prefix}{tag} — <b>{signal.title[:80]}</b>",
        f"   {signal.venue.value.upper()} · {signal.side} @ {signal.entry_price:.3f}",
        f"   edge <b>{signal.edge * 100:+.2f}%</b> net · "
        f"{signal.days_to_resolution:.1f}d · score {signal.score:.4f}",
    ]
    if signal.advisory:
        lines.append("   <i>no position size — this is a prompt to go look, "
                     "not a trade</i>")
    elif signal.stake:
        lines.append(f"   stake <b>${signal.stake:,.2f}</b> "
                     f"({signal.contracts:,.0f} contracts)")

    r = signal.rationale
    if signal.strategy == "combinatorial_arb":
        lines.append(
            f"   {r.get('legs')} legs · cost ${r.get('cost_at_size', 0):,.2f} "
            f"· fees ${r.get('fees_at_size', 0):,.2f} "
            f"· locked profit ${r.get('net_profit', 0):,.2f}"
        )
        if r.get("neg_risk_enforced"):
            lines.append("   ✅ negRisk-enforced on-chain")
    elif signal.strategy == "wallet_attention":
        lines.append(
            f"   {r.get('agreeing_wallets')} qualified wallets · "
            f"their avg entry {r.get('their_avg_entry')} → now "
            f"{r.get('current_price')}"
        )
        lines.append(
            f"   ⚠️ {r.get('move_already_captured_pct')}% of the move already gone"
        )
        for w in (r.get("wallets") or [])[:3]:
            lines.append(
                f"      · {w['user']} — edge {w['entry_adj_edge']:+.3f} "
                f"over {w['n_resolved']} resolved"
            )

    if signal.counter_case:
        lines.append(f"   <i>{signal.counter_case[:280]}</i>")
    return "\n".join(lines)


def build_briefing(signals: list[Signal], state: DisciplineState,
                   calibration_verdict: str = "") -> str:
    """Daily digest, sectioned by resolution horizon."""
    out = ["<b>EDGE ENGINE — DAILY BRIEFING</b>", "", state.status_line(), ""]

    if state.circuit_breaker_tripped:
        out += [
            "🛑 <b>CIRCUIT BREAKER TRIPPED</b>",
            f"Down {state.weekly_drawdown_pct:.1f}% this week. No trades today.",
            "This rule exists because it works when you least want it to.", "",
        ]
        return "\n".join(out)

    if not signals:
        out += [
            "No opportunities cleared the threshold today.", "",
            "<i>This is a normal and correct outcome. A system that finds a trade "
            "every single day is not finding edge, it is lowering its standards.</i>",
        ]
        return "\n".join(out)

    ok, why = state.can_trade()
    if not ok:
        out += [f"⚠️ <b>{why}</b>", "", "Listed for information only:", ""]

    index = 0
    for label, low, high in HORIZONS:
        section = [s for s in signals if low <= s.days_to_resolution < high]
        if not section:
            continue
        out.append(f"<b>═══ {label} ({len(section)}) ═══</b>")
        for signal in section:
            index += 1
            out.append(format_signal(signal, index))
            out.append("")

    if calibration_verdict:
        out += ["<b>─── CALIBRATION ───</b>", calibration_verdict, ""]

    out.append(
        "<i>Order tickets only. Nothing here is placed for you, and nothing here "
        "is advice. Verify every price before you click.</i>"
    )
    return "\n".join(out)
