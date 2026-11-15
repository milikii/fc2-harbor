"""Manual magnet-submission bot — spec 10.

Flow: whitelist check → magnet parse → FC2 normalisation → the same content
gate as the subscription bot → category buttons when the category is ambiguous →
accepted entry + download job → qBittorrent.

The bot holds no business logic: :class:`ManualIngestService` does the work, so
the same path is exercised by tests without aiogram.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from app.bots.commands import WhitelistFilter
from app.bots.keyboards import category_keyboard
from app.logging import error_detail, get_logger
from app.services.manual_ingest import ManualIngestService
from app.services.normalize import extract_magnet

_log = get_logger("bot.manual")

PENDING_TTL_SECONDS = 900.0
MAX_PENDING = 200


@dataclass(frozen=True, slots=True)
class PendingSubmission:
    token: str
    text: str
    user_id: int
    created_at: float
    display_code: str | None = None


class PendingSubmissionStore:
    """Short-lived in-memory map for the "choose a category" step.

    Deliberately not persisted: the submission has not been accepted yet, and an
    unconfirmed submission must not create rows. A restart simply asks the user
    to send the magnet again — which is safe, because nothing was written.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PENDING_TTL_SECONDS,
        max_items: int = MAX_PENDING,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_items = max_items
        self._clock = clock or time.monotonic
        self._items: dict[str, PendingSubmission] = {}

    def add(self, *, text: str, user_id: int, display_code: str | None = None) -> PendingSubmission:
        self._purge()
        while len(self._items) >= self.max_items:
            oldest = min(self._items.values(), key=lambda item: item.created_at)
            self._items.pop(oldest.token, None)
        token = secrets.token_urlsafe(6)
        pending = PendingSubmission(
            token=token,
            text=text,
            user_id=user_id,
            created_at=self._clock(),
            display_code=display_code,
        )
        self._items[token] = pending
        return pending

    def pop(self, token: str, user_id: int) -> PendingSubmission | None:
        self._purge()
        pending = self._items.get(token)
        if pending is None or pending.user_id != user_id:
            return None
        return self._items.pop(token)

    def _purge(self) -> None:
        cutoff = self._clock() - self.ttl_seconds
        for token in [key for key, item in self._items.items() if item.created_at < cutoff]:
            self._items.pop(token, None)

    def __len__(self) -> int:
        return len(self._items)


async def reject_non_magnet(message: Message, text: str) -> None:
    """Handle text without a magnet: always log, answer only on a near miss.

    "I sent something and nothing happened" must be answerable from the logs.
    """

    lowered = text.lower()
    _log.info(
        "bot.manual.no_magnet",
        user_id=message.from_user.id if message.from_user else 0,
        chat_id=message.chat.id,
        preview=_preview(text),
        hint_like="magnet" in lowered or "fc2" in lowered,
    )
    if "magnet" in lowered or "fc2" in lowered:
        await message.answer("没有找到 magnet 链接。请直接发送完整的 magnet:?xt=... 链接。")


def _preview(text: str, limit: int = 200) -> str:
    """A log-safe excerpt: collapsed whitespace, truncated, secrets redacted."""

    from app.logging import redact_text

    return redact_text(" ".join(text.split()))[:limit]


def build_manual_router(
    *,
    ingest: ManualIngestService,
    store: PendingSubmissionStore,
    allowed_user_ids: Iterable[int],
) -> Router:
    router = Router(name="manual-ingest")
    router.message.filter(WhitelistFilter(allowed_user_ids))
    router.callback_query.filter(WhitelistFilter(allowed_user_ids))

    @router.message(F.text & ~F.text.startswith("/"))
    async def handle_text(message: Message) -> None:
        text = message.text or ""
        if extract_magnet(text) is None:
            await reject_non_magnet(message, text)
            return

        user_id = message.from_user.id if message.from_user else 0
        result = await ingest.ingest(text=text, user_id=user_id)

        if result.needs_category and result.magnet is not None:
            pending = store.add(text=text, user_id=user_id, display_code=result.display_code)
            await message.answer(
                "没有识别到 FC2 编号，无法自动分类。\n"
                f"info hash：{result.magnet.info_hash[:12]}…\n请选择分类：",
                reply_markup=category_keyboard(pending.token),
            )
            return

        await message.answer(result.message)

    @router.callback_query(F.data.startswith("cat:"))
    async def handle_category(callback: CallbackQuery) -> None:
        # Answer first: Telegram clients time out quickly (spec 9.3 pattern).
        await callback.answer()
        payload = callback.data or ""
        parts = payload.split(":", 2)
        if len(parts) != 3:
            return
        _prefix, token, category = parts
        user_id = callback.from_user.id if callback.from_user else 0
        pending = store.pop(token, user_id)
        if pending is None:
            await _safe_edit(callback, "分类选择已过期，请重新发送磁链。")
            return
        if category == "cancel":
            await _safe_edit(callback, "已取消。")
            return

        result = await ingest.ingest(text=pending.text, user_id=user_id, category=category)
        await _safe_edit(callback, result.message)

    return router


async def _safe_edit(callback: CallbackQuery, text: str) -> None:
    message = callback.message
    if message is None:
        return
    try:
        await message.edit_text(text)  # type: ignore[union-attr]
    except Exception as exc:
        _log.warning(
            "bot.edit_failed", error_code=type(exc).__name__, error_detail=error_detail(exc)
        )
