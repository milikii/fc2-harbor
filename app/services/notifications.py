"""Notification rows and message-group deletion — spec 9.1 / 9.4 / 12.8.

A notification row is created before the card is sent and updated as each message
id comes back, so a crash mid-send can resume instead of re-posting the whole card.

Deletion is deliberately decoupled from the blacklist write: Telegram may fail, and
that must never roll back the user's decision (spec 9.4). Failures go to a retry
queue that runs in the background.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import NotificationStatus
from app.db.models import Notification, utcnow
from app.db.session import Database
from app.logging import error_detail, get_logger

_log = get_logger("notifications")

DELETE_ATTEMPTS = 3
DELETE_BACKOFF_SECONDS = 5.0

#: The message is not there any more (deleted by the user, or by a previous
#: attempt): the goal is already met, so this counts as success, not failure.
ALREADY_GONE_MARKERS: tuple[str, ...] = ("message to delete not found",)

#: Telegram error texts that will never succeed on retry. Matching the message is
#: deliberate: aiogram turns them all into ``TelegramBadRequest``, so the exception
#: type alone cannot tell a permanent refusal from a transient one.
PERMANENT_DELETE_ERRORS: tuple[str, ...] = (
    "message can't be deleted",
    "message is too old",
    "message_id_invalid",
    "not enough rights",
    "chat not found",
    "bot was blocked by the user",
    "user is deactivated",
)


def is_already_gone(exc: BaseException) -> bool:
    """True when the message does not exist any more — deletion already happened."""

    text = str(exc).lower()
    return any(marker in text for marker in ALREADY_GONE_MARKERS)


def is_permanent_delete_error(exc: BaseException) -> bool:
    """True when retrying cannot help (too old, no rights, chat gone)."""

    text = str(exc).lower()
    return any(marker in text for marker in PERMANENT_DELETE_ERRORS)


async def get_or_create(
    session: AsyncSession, *, content_id: str, bot_name: str, chat_id: int
) -> Notification:
    notification = await session.scalar(
        select(Notification).where(
            Notification.content_id == content_id,
            Notification.bot_name == bot_name,
            Notification.chat_id == chat_id,
        )
    )
    if notification is not None:
        return notification
    notification = Notification(
        content_id=content_id,
        bot_name=bot_name,
        chat_id=chat_id,
        status=NotificationStatus.PENDING,
    )
    session.add(notification)
    await session.flush()
    return notification


async def record_progress(
    session: AsyncSession,
    notification_id: str,
    *,
    poster_message_id: int | None = None,
    description_message_ids: list[int] | None = None,
    media_message_ids: list[int] | None = None,
) -> None:
    notification = await session.get(Notification, notification_id)
    if notification is None:
        return
    if poster_message_id is not None:
        notification.poster_message_id = poster_message_id
    if description_message_ids:
        notification.description_message_ids_json = [
            *(notification.description_message_ids_json or []),
            *description_message_ids,
        ]
    if media_message_ids:
        notification.media_message_ids_json = [
            *(notification.media_message_ids_json or []),
            *media_message_ids,
        ]


async def mark_sent(session: AsyncSession, notification_id: str) -> None:
    notification = await session.get(Notification, notification_id)
    if notification is not None:
        notification.status = NotificationStatus.SENT


async def mark_deleted(session: AsyncSession, notification_id: str) -> None:
    notification = await session.get(Notification, notification_id)
    if notification is not None:
        notification.status = NotificationStatus.DELETED
        notification.deleted_at = utcnow()


async def mark_failed(
    session: AsyncSession, notification_id: str, *, reason: str | None = None
) -> None:
    notification = await session.get(Notification, notification_id)
    if notification is not None:
        notification.status = NotificationStatus.FAILED
        if reason:
            notification.delete_error = reason[:255]


async def load_notification(session: AsyncSession, notification_id: str) -> Notification | None:
    return await session.get(Notification, notification_id)


def message_ids(notification: Notification) -> list[int]:
    """Every message that belongs to the card, in send order."""

    ids: list[int] = []
    if notification.poster_message_id:
        ids.append(notification.poster_message_id)
    ids.extend(notification.description_message_ids_json or [])
    ids.extend(notification.media_message_ids_json or [])
    return ids


@dataclass(slots=True)
class DeletionTask:
    chat_id: int
    message_ids: list[int]
    notification_id: str | None = None
    attempts: int = 0
    last_error: str | None = None
    #: The exception text, so the log explains *why* without a reproduction.
    last_error_detail: str | None = None


@dataclass(slots=True)
class DeletionReport:
    deleted: int = 0
    failed: int = 0
    dropped: int = 0


@dataclass
class MessageDeleter:
    """Deletes a message group, retrying without ever touching the blacklist."""

    db: Database
    bot: object
    max_attempts: int = DELETE_ATTEMPTS
    backoff_seconds: float = DELETE_BACKOFF_SECONDS
    queue: list[DeletionTask] = field(default_factory=list)

    def enqueue(self, *, chat_id: int, ids: list[int], notification_id: str | None = None) -> None:
        if not ids:
            return
        self.queue.append(
            DeletionTask(
                chat_id=chat_id,
                message_ids=list(dict.fromkeys(ids)),
                notification_id=notification_id,
            )
        )

    async def run_once(self) -> DeletionReport:
        report = DeletionReport()
        pending = list(self.queue)
        self.queue.clear()
        for task in pending:
            failures: list[int] = []
            permanent: str | None = None
            already_gone: list[int] = []
            for message_id in task.message_ids:
                try:
                    await self.bot.delete_message(task.chat_id, message_id)  # type: ignore[attr-defined]
                except Exception as exc:
                    task.last_error = type(exc).__name__
                    task.last_error_detail = error_detail(exc)
                    if is_already_gone(exc):
                        # Nothing to delete: the card is already gone, which is the
                        # outcome we wanted. Not a failure, not a retry.
                        already_gone.append(message_id)
                        continue
                    failures.append(message_id)
                    if permanent is None and is_permanent_delete_error(exc):
                        # Telegram will refuse this one forever: stop retrying and
                        # record the reason instead of hammering the API.
                        permanent = _describe_delete_error(exc)
            if not failures:
                report.deleted += 1
                if already_gone:
                    _log.info(
                        "notifications.delete_already_gone",
                        notification_id=task.notification_id,
                        message_ids=already_gone,
                    )
                if task.notification_id:
                    async with self.db.transaction() as session:
                        await mark_deleted(session, task.notification_id)
                continue

            task.message_ids = failures
            task.attempts += 1
            if permanent is not None:
                report.dropped += 1
                _log.error(
                    "notifications.delete_permanent_failure",
                    notification_id=task.notification_id,
                    remaining=len(failures),
                    error_code=task.last_error,
                    error_detail=task.last_error_detail,
                    reason=permanent,
                )
                if task.notification_id:
                    async with self.db.transaction() as session:
                        await mark_failed(
                            session, task.notification_id, reason=permanent
                        )
                continue
            if task.attempts >= self.max_attempts:
                report.dropped += 1
                _log.error(
                    "notifications.delete_gave_up",
                    notification_id=task.notification_id,
                    remaining=len(failures),
                    error_code=task.last_error,
                    error_detail=task.last_error_detail,
                )
                if task.notification_id:
                    async with self.db.transaction() as session:
                        await mark_failed(
                            session,
                            task.notification_id,
                            reason=f"gave up after {task.attempts} attempts: {task.last_error}",
                        )
                continue

            report.failed += 1
            self.queue.append(task)
            _log.warning(
                "notifications.delete_retry",
                notification_id=task.notification_id,
                remaining=len(failures),
                attempts=task.attempts,
                error_code=task.last_error,
                error_detail=task.last_error_detail,
            )
        return report

    async def run(self, stop_event: asyncio.Event, *, interval_seconds: float = 10.0) -> None:
        while not stop_event.is_set():
            if self.queue:
                try:
                    await self.run_once()
                except Exception:
                    _log.exception("notifications.delete_loop_error")
                await asyncio.sleep(self.backoff_seconds)
                continue
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            except TimeoutError:
                continue


def _describe_delete_error(exc: BaseException) -> str:
    """A short, storable reason: the Telegram text, trimmed."""

    text = " ".join(str(exc).split())
    if not text:
        return type(exc).__name__
    return text[:255]


def latest_deleted_at(notification: Notification) -> datetime | None:
    return notification.deleted_at
