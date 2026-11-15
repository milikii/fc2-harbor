"""Notification rows and group deletion — spec 9.1 / 9.4 / 12.8."""

from __future__ import annotations

from app.db.enums import ContentType, NotificationStatus
from app.db.models import Content, Notification
from app.db.session import Database
from app.services import notifications as store
from app.services.notifications import MessageDeleter


class FakeBot:
    def __init__(self, *, fail: bool = False, error: str = "timed out") -> None:
        self.deleted: list[tuple[int, int]] = []
        self.fail = fail
        self.error = error
        self.calls = 0

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError(self.error)
        self.deleted.append((chat_id, message_id))


async def seed_content(db: Database) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
        )
        session.add(content)
        await session.flush()
        return content.id


async def test_get_or_create_is_idempotent(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        first = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        first_id = first.id
    async with db.transaction() as session:
        second = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
    assert second.id == first_id


async def test_progress_appends_message_ids(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id
        await store.record_progress(session, notification_id, poster_message_id=10)
        await store.record_progress(session, notification_id, description_message_ids=[11, 12])
        await store.record_progress(session, notification_id, media_message_ids=[13, 14])
        await store.mark_sent(session, notification_id)

    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.status is NotificationStatus.SENT
    assert store.message_ids(notification) == [10, 11, 12, 13, 14]


async def test_deleter_removes_the_whole_group(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id
        await store.record_progress(
            session, notification_id, poster_message_id=10, media_message_ids=[11, 12]
        )

    bot = FakeBot()
    deleter = MessageDeleter(db=db, bot=bot)
    deleter.enqueue(chat_id=111, ids=[10, 11, 12], notification_id=notification_id)

    report = await deleter.run_once()

    assert report.deleted == 1
    assert bot.deleted == [(111, 10), (111, 11), (111, 12)]
    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.status is NotificationStatus.DELETED
    assert notification.deleted_at is not None


async def test_deleter_retries_then_gives_up(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id

    bot = FakeBot(fail=True)
    deleter = MessageDeleter(db=db, bot=bot, max_attempts=2)
    deleter.enqueue(chat_id=111, ids=[10, 11], notification_id=notification_id)

    first = await deleter.run_once()
    assert first.failed == 1
    assert len(deleter.queue) == 1

    second = await deleter.run_once()
    assert second.dropped == 1
    assert deleter.queue == []
    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.status is NotificationStatus.FAILED


async def test_empty_enqueue_is_ignored(db: Database) -> None:
    deleter = MessageDeleter(db=db, bot=FakeBot())
    deleter.enqueue(chat_id=111, ids=[])
    assert deleter.queue == []


async def test_duplicate_ids_are_sent_once(db: Database) -> None:
    bot = FakeBot()
    deleter = MessageDeleter(db=db, bot=bot)
    deleter.enqueue(chat_id=111, ids=[5, 5, 6])
    await deleter.run_once()
    assert bot.deleted == [(111, 5), (111, 6)]


# -- permanent vs transient deletion failures --------------------------------


async def test_permanent_delete_error_stops_after_one_attempt(db: Database) -> None:
    """Telegram refuses forever ("message can't be deleted"): do not hammer it."""

    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id

    bot = FakeBot(fail=True, error="Telegram server says - Bad Request: message can't be deleted")
    deleter = MessageDeleter(db=db, bot=bot, max_attempts=3)
    deleter.enqueue(chat_id=111, ids=[10, 11], notification_id=notification_id)

    report = await deleter.run_once()

    assert report.dropped == 1
    assert report.failed == 0
    assert deleter.queue == []  # no retry scheduled
    assert bot.calls == 2  # one attempt per message id, no repeats
    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.status is NotificationStatus.FAILED
    assert notification.delete_error is not None
    assert "can't be deleted" in notification.delete_error


async def test_message_already_gone_is_permanent(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id

    bot = FakeBot(fail=True, error="Bad Request: message to delete not found")
    deleter = MessageDeleter(db=db, bot=bot)
    deleter.enqueue(chat_id=111, ids=[10], notification_id=notification_id)

    report = await deleter.run_once()

    assert report.dropped == 1
    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.delete_error == "Bad Request: message to delete not found"


async def test_transient_error_is_retried_and_the_reason_is_stored(db: Database) -> None:
    content_id = await seed_content(db)
    async with db.transaction() as session:
        notification = await store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        notification_id = notification.id

    bot = FakeBot(fail=True, error="timed out")
    deleter = MessageDeleter(db=db, bot=bot, max_attempts=2)
    deleter.enqueue(chat_id=111, ids=[10], notification_id=notification_id)

    assert (await deleter.run_once()).failed == 1
    assert len(deleter.queue) == 1

    assert (await deleter.run_once()).dropped == 1
    async with db.session() as session:
        notification = await session.get(Notification, notification_id)
    assert notification is not None
    assert notification.status is NotificationStatus.FAILED
    assert notification.delete_error is not None
    assert "gave up after 2 attempts" in notification.delete_error
    assert "RuntimeError" in notification.delete_error


def test_permanent_error_detection() -> None:
    from app.services.notifications import is_permanent_delete_error

    assert is_permanent_delete_error(RuntimeError("message to delete not found"))
    assert is_permanent_delete_error(RuntimeError("Bad Request: message can't be deleted"))
    assert is_permanent_delete_error(RuntimeError("message is too old"))
    assert is_permanent_delete_error(RuntimeError("Not enough rights to delete messages"))
    assert not is_permanent_delete_error(RuntimeError("timed out"))
    assert not is_permanent_delete_error(RuntimeError("Internal Server Error"))
