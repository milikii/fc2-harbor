"""Subscription button actions — spec 9.3 / 9.4.

Fake downloader, real database: the transactional behaviour is what matters here
(one job per click, accepted entry written first, blacklist never rolled back by a
Telegram failure).
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.config import Settings
from app.db.enums import (
    AcceptedOrigin,
    ContentType,
    DownloaderName,
    DownloadStatus,
    GateReason,
)
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    DecisionEvent,
    DownloadJob,
    Notification,
    Release,
)
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.services import notifications as notification_store
from app.services.acceptance import AcceptOutcome, SubscriptionActions
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from sqlalchemy import func, select

from tests.support import FakeDownloader, hex_hash

HASH = hex_hash(901)


async def seed_content(db: Database, *, blacklisted: bool = False) -> tuple[str, str]:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
            title_ja="サンプル作品",
        )
        session.add(content)
        await session.flush()
        release = Release(
            content_id=content.id,
            source="nyaa",
            source_item_id="4761220",
            source_url="https://sukebei.nyaa.si/view/4761220",
            magnet=f"magnet:?xt=urn:btih:{HASH}",
            info_hash=HASH,
            published_at=datetime.now(UTC),
        )
        session.add(release)
        if blacklisted:
            session.add(BlacklistEntry(canonical_key=content.canonical_key, reason="test"))
        await session.flush()
        return content.id, release.id


def build(db: Database, settings: Settings, downloader: FakeDownloader) -> SubscriptionActions:
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: downloader})
    submitter = JobSubmitter(db=db, registry=registry, settings=settings)
    return SubscriptionActions(db=db, gate=ContentGate(), submitter=submitter, settings=settings)


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_first_click_creates_one_job(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _release_id = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    result = await actions.accept(content_id, actor="telegram:111")

    assert result.outcome is AcceptOutcome.SUBMITTED
    assert result.job_id is not None
    assert len(fake_downloader.added) == 1
    assert await _count(db, DownloadJob) == 1
    assert await _count(db, AcceptedEntry) == 1

    async with db.session() as session:
        entry = await session.scalar(select(AcceptedEntry))
        job = await session.scalar(select(DownloadJob))
    assert entry is not None and entry.origin is AcceptedOrigin.SUBSCRIPTION_BUTTON
    assert job is not None and job.status is DownloadStatus.SUBMITTED


async def test_second_click_is_idempotent(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    first = await actions.accept(content_id, actor="telegram:111")
    second = await actions.accept(content_id, actor="telegram:111")

    assert first.outcome is AcceptOutcome.SUBMITTED
    assert second.outcome is AcceptOutcome.ALREADY
    assert second.job_id == first.job_id
    assert len(fake_downloader.added) == 1
    assert await _count(db, DownloadJob) == 1


async def test_blacklisted_content_cannot_be_downloaded(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db, blacklisted=True)
    actions = build(db, settings, fake_downloader)

    result = await actions.accept(content_id, actor="telegram:111")

    assert result.outcome is AcceptOutcome.BLACKLISTED
    assert fake_downloader.added == []
    assert await _count(db, DownloadJob) == 0


async def test_failed_submission_keeps_the_accepted_entry(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.add_should_fail = True
    content_id, _ = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    result = await actions.accept(content_id, actor="telegram:111")

    assert result.outcome is AcceptOutcome.FAILED
    assert await _count(db, AcceptedEntry) == 1
    async with db.session() as session:
        job = await session.scalar(select(DownloadJob))
    assert job is not None and job.status is DownloadStatus.FAILED

    # A second click does not add a second task.
    again = await actions.accept(content_id, actor="telegram:111")
    assert again.outcome is AcceptOutcome.ALREADY


async def test_missing_content_is_reported(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    actions = build(db, settings, fake_downloader)
    result = await actions.accept("does-not-exist", actor="telegram:111")
    assert result.outcome is AcceptOutcome.MISSING


async def test_block_writes_blacklist_and_returns_message_ids(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    async with db.transaction() as session:
        notification = await notification_store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        await notification_store.record_progress(
            session,
            notification.id,
            poster_message_id=10,
            media_message_ids=[11, 12],
        )

    actions = build(db, settings, fake_downloader)
    result = await actions.block(content_id, actor="telegram:111", chat_id=111)

    assert result.blocked is True
    assert result.already_blocked is False
    assert result.message_ids == [10, 11, 12]
    assert result.notification_id is not None
    assert await _count(db, BlacklistEntry) == 1

    async with db.session() as session:
        event = await session.scalar(
            select(DecisionEvent).where(DecisionEvent.event_type == "content.blacklisted")
        )
    assert event is not None and event.actor == "telegram:111"


async def test_second_block_is_recorded_as_already_blocked(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    first = await actions.block(content_id, actor="telegram:111", chat_id=111)
    second = await actions.block(content_id, actor="telegram:111", chat_id=111)

    assert first.already_blocked is False
    assert second.already_blocked is True
    assert await _count(db, BlacklistEntry) == 1


async def test_blocked_content_then_download_is_rejected(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    await actions.block(content_id, actor="telegram:111", chat_id=111)
    result = await actions.accept(content_id, actor="telegram:111")

    assert result.outcome is AcceptOutcome.BLACKLISTED
    assert result.message == "该编号已拉黑"


async def test_block_returns_no_ids_without_notification(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions = build(db, settings, fake_downloader)

    result = await actions.block(content_id, actor="telegram:111", chat_id=111)

    assert result.blocked is True
    assert result.message_ids == []
    assert result.notification_id is None
    assert await _count(db, Notification) == 0


def test_gate_reason_enum_is_stable() -> None:
    # The bot maps reasons to Chinese text; a rename would break that mapping.
    assert GateReason.ALREADY_ACCEPTED.value == "already_accepted"
    assert GateReason.BLACKLISTED.value == "blacklisted"
