"""One-shot Telegram wiring.

    python -m edge_engine.setup_telegram

Reads the bot token from the TELEGRAM_BOT_TOKEN environment variable (set by
secrets.local.ps1) or, as a fallback, config.yaml. Auto-detects your chat id
from the bot's pending updates, writes it back to whichever file supplied the
token, and sends a test message.

Your token stays on this machine and is never printed in full.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .ingest.http import request_json

CONFIG = Path("config.yaml")
SECRETS = Path("secrets.local.ps1")


def _token_from_secrets(text: str) -> str | None:
    match = re.search(r'TELEGRAM_BOT_TOKEN\s*=\s*"([^"]*)"', text)
    if not match:
        return None
    value = match.group(1).strip()
    return None if not value or "paste-your" in value else value


def _token_from_config(text: str) -> str | None:
    match = re.search(r"^telegram_bot_token:\s*(.+)$", text, re.M)
    if not match:
        return None
    value = match.group(1).strip().strip("'\"")
    return None if value in ("null", "~", "") else value


def _write_chat_id_to_secrets(text: str, chat_id: str) -> str:
    line = f'$env:TELEGRAM_CHAT_ID   = "{chat_id}"'
    if re.search(r"\$env:TELEGRAM_CHAT_ID\s*=", text):
        return re.sub(r'\$env:TELEGRAM_CHAT_ID\s*=\s*"[^"]*"', line, text)
    return text.rstrip() + "\n" + line + "\n"


def _write_chat_id_to_config(text: str, chat_id: str) -> str:
    if re.search(r"^telegram_chat_id:", text, re.M):
        return re.sub(r"^telegram_chat_id:.*$",
                      f"telegram_chat_id: '{chat_id}'", text, flags=re.M)
    return text + f"\ntelegram_chat_id: '{chat_id}'\n"


def _explain_missing() -> None:
    print("ERROR: no Telegram bot token found.\n")
    print("  1. In Telegram, message @BotFather and send /newbot")
    print("     (or /revoke on an existing bot to get a fresh token)")
    print("  2. Tap the token to copy the WHOLE thing")
    print(f"  3. Paste it into {SECRETS} between the quotes on the")
    print("     TELEGRAM_BOT_TOKEN line, then save")
    print("  4. Send your bot any message, e.g. 'hi' (it will NOT reply -")
    print("     that is normal, nothing is listening for incoming messages)")
    print("  5. Run this again")


def main() -> int:
    # Environment first: that is where secrets.local.ps1 and the GitHub Actions
    # secrets both land. config.yaml is only a fallback and should stay empty.
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    target = "env"

    if not token and SECRETS.exists():
        token = _token_from_secrets(SECRETS.read_text(encoding="utf-8"))
        target = "secrets"
    if not token and CONFIG.exists():
        token = _token_from_config(CONFIG.read_text(encoding="utf-8"))
        target = "config"
    if token and target == "env" and SECRETS.exists():
        target = "secrets"   # prefer writing the chat id where the token lives

    if not token:
        _explain_missing()
        return 1

    print(f"Token found (...{token[-4:]}). Checking with Telegram...")
    try:
        me = request_json(f"https://api.telegram.org/bot{token}/getMe")
    except Exception as e:
        print(f"\nERROR: Telegram rejected this token.\n  {e}\n")
        secret_len = len(token.split(":")[-1])
        if secret_len != 35:
            print(f"  The part after the ':' is {secret_len} characters; it "
                  f"should be 35.")
            print("  It looks truncated - copy the token again by TAPPING it in")
            print("  Telegram rather than selecting it by hand.")
        else:
            print("  Length looks right, so the token may have been revoked.")
            print("  Send /revoke to @BotFather to issue a fresh one.")
        return 1
    if not me.get("ok"):
        print(f"ERROR: {me}")
        return 1
    username = me["result"].get("username")
    print(f"Bot OK: @{username}")

    updates = request_json(f"https://api.telegram.org/bot{token}/getUpdates")
    chats = []
    for update in (updates.get("result") or []):
        message = update.get("message") or update.get("channel_post") or {}
        chat = message.get("chat") or {}
        if chat.get("id") is not None and chat["id"] not in [c[0] for c in chats]:
            name = (chat.get("username") or chat.get("first_name")
                    or chat.get("title") or "?")
            chats.append((chat["id"], name))

    if not chats:
        print(f"\nNo messages found from you yet.")
        print(f"  -> In Telegram, open @{username} and send it any message.")
        print(f"     It will NOT reply - nothing is listening for incoming")
        print(f"     messages. The message just needs to exist so this can read")
        print(f"     your chat id. Then run this again.")
        return 1

    chat_id, name = chats[0]
    if len(chats) > 1:
        print(f"Found {len(chats)} chats, using the first: {name}")
    print(f"Chat id: {chat_id} ({name})")

    if target == "config" and CONFIG.exists():
        CONFIG.write_text(
            _write_chat_id_to_config(CONFIG.read_text(encoding="utf-8"),
                                     str(chat_id)), encoding="utf-8")
        print(f"Wrote telegram_chat_id to {CONFIG}")
    elif SECRETS.exists():
        SECRETS.write_text(
            _write_chat_id_to_secrets(SECRETS.read_text(encoding="utf-8"),
                                      str(chat_id)), encoding="utf-8")
        print(f"Wrote TELEGRAM_CHAT_ID to {SECRETS}")
    else:
        print(f"Set TELEGRAM_CHAT_ID={chat_id} in your environment.")

    request_json(
        f"https://api.telegram.org/bot{token}/sendMessage"
        f"?chat_id={chat_id}&parse_mode=HTML"
        f"&text=%E2%9C%85%20%3Cb%3Eedge-engine%20connected%3C%2Fb%3E%0A"
        f"Alerts%20will%20arrive%20here.%20Nothing%20is%20ever%20traded%20"
        f"automatically%20-%20every%20alert%20is%20an%20order%20ticket%20"
        f"you%20place%20yourself."
    )
    print("\nTest message sent. Check Telegram.")
    print('Next: double-click "4 - RUN A SCAN NOW.bat"')
    return 0


if __name__ == "__main__":
    sys.exit(main())
