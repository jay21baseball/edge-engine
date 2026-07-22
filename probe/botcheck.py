"""Run the /dailyedge handler directly, outside the polling loop."""
import logging
import traceback

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

from edge_engine.bot.commands import HANDLERS
from edge_engine.scan import Engine, load_config

config = load_config()
engine = Engine(config)
chat_id = str(config.get("telegram_chat_id") or "test")
print(f"chat_id={chat_id!r}  token={'set' if config.get('telegram_bot_token') else 'MISSING'}")
print(f"has_bankroll={engine.has_bankroll(chat_id)}")
print(f"minutes_since_scan={engine.minutes_since_scan()}")
print()

try:
    reply = HANDLERS["dailyedge"](engine, chat_id, [], lambda m: print(f"[progress] {m}"))
    print("--- HANDLER RETURNED ---")
    print((reply or "<empty>")[:1200])
except Exception:
    print("--- HANDLER RAISED ---")
    traceback.print_exc()
