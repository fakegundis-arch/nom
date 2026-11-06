"""Telegram alerts and a few read-only commands (/status, /balance, /help)."""

import logging
from typing import Callable

import requests

log = logging.getLogger(__name__)


class Telegram:
    def __init__(self, bot_token: str, chat_id: str, session: requests.Session | None = None):
        self.base = f"https://api.telegram.org/bot{bot_token}"
        self.chat_id = str(chat_id)
        self.http = session or requests.Session()
        self._offset = 0

    def send(self, text: str) -> None:
        try:
            self.http.post(self.base + "/sendMessage",
                           json={"chat_id": self.chat_id, "text": text}, timeout=15).raise_for_status()
        except requests.RequestException as exc:
            # An alert failing must never crash the bot.
            log.error("Telegram send failed: %s", exc)

    def handle_commands(self, handlers: dict[str, Callable[[str], str]]) -> None:
        """Answer commands from YOUR chat only; messages from anyone else are ignored."""
        try:
            resp = self.http.get(self.base + "/getUpdates",
                                 params={"offset": self._offset, "timeout": 0}, timeout=15)
            resp.raise_for_status()
            updates = resp.json().get("result", [])
        except requests.RequestException as exc:
            log.warning("Telegram getUpdates failed: %s", exc)
            return
        for update in updates:
            self._offset = update["update_id"] + 1
            message = update.get("message") or {}
            if str(message.get("chat", {}).get("id")) != self.chat_id:
                continue
            words = (message.get("text") or "").split(maxsplit=1)
            if not words:
                continue
            command = words[0].split("@")[0].lower()
            handler = handlers.get(command)
            if handler:
                self.send(handler(words[1] if len(words) > 1 else ""))
