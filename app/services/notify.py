"""Admin notifications for failures that need a human (spec 16.3).

The organizer and monitor never swallow a failure: they record it in the
database and tell the administrator. In M1 the Telegram implementation uses the
manual bot; the subscription bot's full card pipeline arrives in M4.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from app.logging import error_detail, get_logger

_log = get_logger("notify")

MAX_TELEGRAM_MESSAGE = 4000


class AdminNotifier(Protocol):
    async def notify(self, text: str) -> None:
        """Send a short notice to the administrator; never raise."""


class LogNotifier:
    """Fallback used in tests and when no bot is configured."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    async def notify(self, text: str) -> None:
        self.messages.append(text)
        _log.warning("admin.notice", text=text)


class TelegramAdminNotifier:
    """Sends notices to the whitelisted private chats."""

    def __init__(self, bot: object, chat_ids: Sequence[int]) -> None:
        self._bot = bot
        self._chat_ids = list(dict.fromkeys(chat_ids))

    async def notify(self, text: str) -> None:
        payload = text[:MAX_TELEGRAM_MESSAGE]
        for chat_id in self._chat_ids:
            try:
                await self._bot.send_message(chat_id, payload)  # type: ignore[attr-defined]
            except Exception as exc:
                _log.error(
                    "notify.telegram_failed",
                    chat_id=chat_id,
                    error_code=type(exc).__name__,
                    error_detail=error_detail(exc),
                )
