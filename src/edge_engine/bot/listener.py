"""Telegram long-polling listener.

Turns the bot from a broadcaster into something you can talk to. Reads updates,
dispatches commands, and holds just enough conversational state to ask for a
bankroll before a briefing and then carry on where it left off.
"""
from __future__ import annotations

import logging
import re
import time
import urllib.parse
from typing import Optional

from ..ingest.http import request_json
from .commands import ALIASES, HANDLERS

log = logging.getLogger(__name__)

TELEGRAM = "https://api.telegram.org"
MAX_MESSAGE = 3900   # Telegram hard-caps at 4096; leave room for the split note

NUMBER = re.compile(r"^\$?\s*([\d,]+(?:\.\d+)?)\s*(k)?$", re.I)


def parse_number(text: str) -> Optional[float]:
    """Accept '2500', '$2,500', '2.5k'."""
    match = NUMBER.match(text.strip())
    if not match:
        return None
    value = float(match.group(1).replace(",", ""))
    return value * 1000 if match.group(2) else value


def split_message(text: str, limit: int = MAX_MESSAGE) -> list[str]:
    """Split on blank lines so an HTML tag never straddles a boundary."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], []
    length = 0
    for block in text.split("\n\n"):
        if length + len(block) + 2 > limit and current:
            chunks.append("\n\n".join(current))
            current, length = [], 0
        current.append(block)
        length += len(block) + 2
    if current:
        chunks.append("\n\n".join(current))
    return chunks


class TelegramBot:
    def __init__(self, engine, token: str, allowed_chat_id: Optional[str] = None):
        self.engine = engine
        self.token = token
        # Only answer the owner. The bot exposes bankroll and P&L, and a bot
        # username is guessable, so an open bot leaks a stranger's view into
        # your positions.
        self.allowed = str(allowed_chat_id) if allowed_chat_id else None
        self._offset: Optional[int] = None

    # ----------------------------------------------------------------- send

    def send(self, chat_id: str, text: str) -> Optional[int]:
        """Send text, returning the message id of the final chunk."""
        message_id = None
        for chunk in split_message(text):
            try:
                result = request_json(
                    f"{TELEGRAM}/bot{self.token}/sendMessage"
                    f"?chat_id={urllib.parse.quote(str(chat_id))}"
                    f"&parse_mode=HTML&disable_web_page_preview=true"
                    f"&text={urllib.parse.quote(chunk)}"
                )
                message_id = (result.get("result") or {}).get("message_id")
            except Exception as e:
                log.error("send failed: %s", e)
                # Retry once without markup - an unbalanced tag is the usual
                # cause and it should not swallow the whole message.
                try:
                    plain = re.sub(r"<[^>]+>", "", chunk)
                    request_json(
                        f"{TELEGRAM}/bot{self.token}/sendMessage"
                        f"?chat_id={urllib.parse.quote(str(chat_id))}"
                        f"&text={urllib.parse.quote(plain)}"
                    )
                except Exception:
                    log.error("plain-text retry also failed")
        return message_id

    # -------------------------------------------------------------- one-time

    def register_command_menu(self) -> None:
        """Populate Telegram's own '/' menu so commands are discoverable."""
        from .commands import COMMAND_MENU
        import json as _json
        try:
            payload = _json.dumps([
                {"command": name, "description": description}
                for name, description in COMMAND_MENU
            ])
            request_json(
                f"{TELEGRAM}/bot{self.token}/setMyCommands"
                f"?commands={urllib.parse.quote(payload)}"
            )
            log.info("registered %d commands in the Telegram menu",
                     len(COMMAND_MENU))
        except Exception as e:
            log.warning("could not register command menu: %s", e)

    def pin_help(self, chat_id: str) -> None:
        """Pin the command list once, so it is always one tap away.

        Re-pinning on every start would spam the chat, so the message id is
        remembered and the pin is skipped if it is already in place.
        """
        from .commands import HELP_TEXT
        existing = self.engine.store.get_state(chat_id, "pinned_help_id")
        if existing:
            return
        try:
            message_id = self.send(chat_id, HELP_TEXT)
            if not message_id:
                return
            request_json(
                f"{TELEGRAM}/bot{self.token}/pinChatMessage"
                f"?chat_id={urllib.parse.quote(str(chat_id))}"
                f"&message_id={message_id}&disable_notification=true"
            )
            self.engine.store.set_state(chat_id, "pinned_help_id", message_id)
            log.info("pinned the command list in chat %s", chat_id)
        except Exception as e:
            log.warning("could not pin help: %s", e)

    # ---------------------------------------------------------------- parse

    @staticmethod
    def parse_command(text: str) -> tuple[Optional[str], list[str]]:
        stripped = text.strip()
        if stripped.startswith("/"):
            parts = stripped[1:].split()
            if not parts:
                return None, []
            name = parts[0].split("@")[0].lower()
            return name, parts[1:]
        lowered = re.sub(r"[^\w\s']", "", stripped.lower()).strip()
        if lowered in ALIASES:
            return ALIASES[lowered], []
        for phrase, command in ALIASES.items():
            if lowered.startswith(phrase):
                return command, lowered[len(phrase):].split()
        return None, stripped.split()

    # -------------------------------------------------------------- dispatch

    def handle(self, chat_id: str, text: str) -> None:
        reply = lambda message: self.send(chat_id, message)
        command, args = self.parse_command(text)

        # A bare number answers a pending "what is your bankroll?" question.
        pending = self.engine.store.get_state(chat_id, "pending")
        if command is None and pending:
            amount = parse_number(text)
            if amount is not None:
                self.send(chat_id, HANDLERS["bankroll"](
                    self.engine, chat_id, [str(amount)], reply))
                self.engine.store.set_state(chat_id, "pending", None)
                follow = "weeklyedge" if "WEEK" in str(pending) else "dailyedge"
                self.send(chat_id, HANDLERS[follow](
                    self.engine, chat_id, [], reply))
                return

        if command is None:
            amount = parse_number(text)
            if amount is not None:
                self.send(chat_id, HANDLERS["bankroll"](
                    self.engine, chat_id, [str(amount)], reply))
                return
            self.send(chat_id, (
                "Not sure what you meant.\n\n"
                "Try <code>/dailyedge</code> for today's plays, or "
                "<code>/help</code> for everything."
            ))
            return

        handler = HANDLERS.get(command)
        if not handler:
            self.send(chat_id, f"Unknown command <code>/{command}</code>. "
                               f"Try <code>/help</code>")
            return

        try:
            response = handler(self.engine, chat_id, args, reply)
        except Exception as e:
            log.exception("handler %s failed", command)
            self.send(chat_id, f"That command failed: {e}\n\n"
                               f"<i>Nothing was traded or changed.</i>")
            return
        if response:
            self.send(chat_id, response)

    # ------------------------------------------------------------------ loop

    def poll_once(self, timeout: int = 25) -> int:
        updates = request_json(
            f"{TELEGRAM}/bot{self.token}/getUpdates"
            f"?timeout={timeout}"
            + (f"&offset={self._offset}" if self._offset is not None else ""),
            timeout=timeout + 15,
        )
        handled = 0
        for update in (updates.get("result") or []):
            self._offset = update["update_id"] + 1
            message = update.get("message") or update.get("edited_message") or {}
            text = message.get("text")
            chat_id = str((message.get("chat") or {}).get("id", ""))
            if not text or not chat_id:
                continue
            if self.allowed and chat_id != self.allowed:
                log.warning("ignoring message from unauthorized chat %s", chat_id)
                continue
            log.info("<- %s: %s", chat_id, text[:60])
            self.handle(chat_id, text)
            handled += 1
        return handled

    def run(self) -> None:
        self.register_command_menu()
        if self.allowed:
            self.pin_help(self.allowed)
        log.info("bot listening. ctrl-c to stop.")
        while True:
            try:
                self.poll_once()
            except KeyboardInterrupt:
                log.info("bot stopped")
                return
            except Exception as e:
                log.error("poll failed, retrying in 5s: %s", e)
                time.sleep(5)
