"""One-shot Telegram wiring.

    python -m edge_engine.setup_telegram

Reads `telegram_bot_token` from config.yaml, auto-detects your chat id from the
bot's pending updates, writes it back, and sends a test message.

Your token stays on this machine - it is read from the config file and never
printed in full.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from .ingest.http import request_json

CONFIG = Path("config.yaml")


def _read_token(text: str) -> str | None:
    match = re.search(r"^telegram_bot_token:\s*(.+)$", text, re.M)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"")
    return None if value in ("null", "~", "") else value


def _write_chat_id(text: str, chat_id: str) -> str:
    if re.search(r"^telegram_chat_id:", text, re.M):
        return re.sub(r"^telegram_chat_id:.*$",
                      f"telegram_chat_id: '{chat_id}'", text, flags=re.M)
    return text + f"\ntelegram_chat_id: '{chat_id}'\n"


def main() -> int:
    if not CONFIG.exists():
        print(f"ERROR: {CONFIG} not found. Run from the edge-engine directory.")
        return 1

    text = CONFIG.read_text(encoding="utf-8")
    token = _read_token(text)
    if not token:
        print("ERROR: no telegram_bot_token in config.yaml.\n")
        print("  1. Open Telegram, message @BotFather, send /newbot")
        print("  2. Pick any name and a username ending in 'bot'")
        print("  3. Paste the token into config.yaml as:")
        print("       telegram_bot_token: '123456:ABC-your-token-here'")
        print("  4. Send your new bot any message (e.g. 'hi')")
        print("  5. Re-run this command")
        return 1

    print(f"token found (...{token[-6:]}). checking bot...")
    try:
        me = request_json(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as e:
        print(f"ERROR: token rejected by Telegram: {e}")
        return 1
    if not me.get("ok"):
        print(f"ERROR: {me}")
        return 1
    print(f"bot OK: @{me['result'].get('username')}")

    updates = request_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = []
    for update in (updates.get("result") or []):
        message = (update.get("message") or update.get("channel_post") or {})
        chat = message.get("chat") or {}
        if chat.get("id") is not None and chat["id"] not in [c[0] for c in chats]:
            name = (chat.get("username") or chat.get("first_name")
                    or chat.get("title") or "?")
            chats.append((chat["id"], name))

    if not chats:
        print("\nNo messages found yet.")
        print(f"  -> Open Telegram, find @{me['result'].get('username')}, "
              f"send it any message, then re-run this command.")
        return 1

    chat_id, name = chats[0]
    if len(chats) > 1:
        print(f"found {len(chats)} chats, using the first: {name}")
    print(f"chat id: {chat_id} ({name})")

    CONFIG.write_text(_write_chat_id(text, str(chat_id)), encoding="utf-8")
    print("wrote telegram_chat_id to config.yaml")

    request_json(
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={chat_id}&parse_mode=HTML"
        f"&text=%E2%9C%85%20%3Cb%3Eedge-engine%20connected%3C%2Fb%3E%0A"
        f"Alerts%20will%20arrive%20here.%20Nothing%20is%20ever%20traded%20"
        f"automatically%20-%20every%20alert%20is%20an%20order%20ticket%20"
        f"you%20place%20yourself."
    )
    print("\nTest message sent. Check Telegram.")
    print("You're wired. Next:  python -m edge_engine.scan wallets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
