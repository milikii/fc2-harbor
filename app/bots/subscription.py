"""Subscription bot callbacks — spec 9.2 / 9.3 / 9.4.

The handler bodies stay thin: answer the callback immediately (Telegram clients
time out fast), delegate to :class:`SubscriptionActions`, then update the button.
Every handler re-checks the whitelist through the router filter, so hiding a button
is never the only protection.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import F, Router
from aiogram.types import CallbackQuery

from app.bots.commands import WhitelistFilter
from app.bots.keyboards import settled_keyboard
from app.config import Settings
from app.db.session import Database
from app.logging import get_logger
from app.services.acceptance import AcceptOutcome, SubscriptionActions
from app.services.cards import parse_callback
from app.services.notifications import MessageDeleter

_log = get_logger("bot.subscription")

DOWNLOAD_LABELS = {
    AcceptOutcome.SUBMITTED: "已下载",
    AcceptOutcome.ALREADY: "已下载",
    AcceptOutcome.BLACKLISTED: "已拉黑",
    AcceptOutcome.FAILED: "重试",
}


def build_subscription_router(
    *,
    db: Database,
    actions: SubscriptionActions,
    deleter: MessageDeleter,
    settings: Settings,
    allowed_user_ids: Iterable[int],
) -> Router:
    router = Router(name="subscription")
    router.callback_query.filter(WhitelistFilter(allowed_user_ids))

    @router.callback_query(F.data.startswith("dl:"))
    async def handle_download(callback: CallbackQuery) -> None:
        # Answer first: Telegram retries/times out otherwise.
        await callback.answer()
        parsed = parse_callback(callback.data or "")
        if parsed is None or callback.message is None:
            return
        _action, content_id = parsed
        user_id = callback.from_user.id if callback.from_user else 0

        result = await actions.accept(content_id, actor=f"telegram:{user_id}")
        label = DOWNLOAD_LABELS.get(result.outcome, "已处理")

        if result.outcome is AcceptOutcome.FAILED:
            await _safe_reply(callback, result.message)
        elif result.outcome is AcceptOutcome.ALREADY:
            await _safe_reply(callback, "该内容此前已处理，不会重复投递。")
        elif result.outcome is AcceptOutcome.BLACKLISTED:
            await _safe_reply(callback, result.message)

        await _edit_keyboard(callback, content_id, label, settings)

    @router.callback_query(F.data.startswith("blk:"))
    async def handle_block(callback: CallbackQuery) -> None:
        await callback.answer()
        parsed = parse_callback(callback.data or "")
        if parsed is None or callback.message is None:
            return
        _action, content_id = parsed
        user_id = callback.from_user.id if callback.from_user else 0
        chat_id = callback.message.chat.id

        result = await actions.block(content_id, actor=f"telegram:{user_id}", chat_id=chat_id)

        # Deletion is best effort and retried in the background; the blacklist row
        # is already committed (spec 9.4).
        if result.message_ids:
            deleter.enqueue(
                chat_id=chat_id,
                ids=result.message_ids,
                notification_id=result.notification_id,
            )
            report = await deleter.run_once()
            if report.failed:
                _log.warning(
                    "bot.subscription.delete_deferred",
                    content_id=content_id,
                    pending=len(deleter.queue),
                )

        await _edit_keyboard(callback, content_id, "已拉黑", settings)

    return router


async def _edit_keyboard(
    callback: CallbackQuery, content_id: str, label: str, settings: Settings
) -> None:
    message = callback.message
    if message is None:
        return
    try:
        await message.edit_reply_markup(  # type: ignore[union-attr]
            reply_markup=settled_keyboard(
                getattr(message, "reply_markup", None), content_id, None, download_label=label
            )
        )
    except Exception as exc:
        _log.warning("bot.subscription.keyboard_failed", error_code=type(exc).__name__)


async def _safe_reply(callback: CallbackQuery, text: str) -> None:
    message = callback.message
    if message is None or not text:
        return
    try:
        await message.answer(text)  # type: ignore[union-attr]
    except Exception as exc:
        _log.warning("bot.subscription.reply_failed", error_code=type(exc).__name__)
