"""The seven integration tests from spec 22.3.

Each test is named after the sentence in the specification it proves, so the
acceptance document can point at it directly. They are the reason the rest of the
system exists: no duplicate task, no lost file, no premature deletion.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.db.enums import ContentType, DownloaderName, DownloadStatus
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    DownloadJob,
    Release,
)
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.organizer.monitor import DownloadMonitor, reconcile_startup
from app.organizer.mover import OrganizeError, Organizer
from app.organizer.stability import StabilityTracker
from app.services import notifications as notification_store
from app.services.acceptance import AcceptOutcome, SubscriptionActions
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from app.services.manual_ingest import ManualIngestService
from app.services.notify import LogNotifier
from sqlalchemy import func, select

from tests.support import FakeDownloader, hex_hash

S = DownloadStatus


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def seed_content(db: Database, number: str = "1234567") -> tuple[str, str]:
    info_hash = hex_hash(2000 + int(number[-3:]))
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=f"fc2_ppv:{number}",
            display_code=f"FC2-PPV-{number}",
            title_ja="サンプル作品",
            description_ja="説明。",
        )
        session.add(content)
        await session.flush()
        release = Release(
            content_id=content.id,
            source="nyaa",
            source_item_id=f"item-{number}",
            source_url=f"https://sukebei.nyaa.si/view/{number}",
            magnet=f"magnet:?xt=urn:btih:{info_hash}",
            info_hash=info_hash,
            published_at=datetime.now(UTC),
        )
        session.add(release)
        await session.flush()
        return content.id, release.id


def build_actions(
    db: Database, settings: Settings, downloader: FakeDownloader
) -> tuple[SubscriptionActions, JobSubmitter]:
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: downloader})
    submitter = JobSubmitter(db=db, registry=registry, settings=settings)
    return (
        SubscriptionActions(db=db, gate=ContentGate(), submitter=submitter, settings=settings),
        submitter,
    )


def build_monitor(db: Database, settings: Settings, downloader: FakeDownloader) -> DownloadMonitor:
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: downloader})
    return DownloadMonitor(
        db=db,
        registry=registry,
        organizer=Organizer(),
        settings=settings,
        stability=StabilityTracker(required_checks=2, interval_seconds=0),
        notifier=LogNotifier(),
    )


async def job(db: Database, job_id: str) -> DownloadJob:
    async with db.session() as session:
        loaded = await session.get(DownloadJob, job_id)
        assert loaded is not None
        return loaded


async def count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# 22.3.1 clicking download creates exactly one task
# ---------------------------------------------------------------------------


async def test_clicking_download_creates_exactly_one_task(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions, _submitter = build_actions(db, settings, fake_downloader)

    result = await actions.accept(content_id, actor="telegram:111")

    assert result.outcome is AcceptOutcome.SUBMITTED
    assert len(fake_downloader.added) == 1
    assert await count(db, DownloadJob) == 1
    assert await count(db, AcceptedEntry) == 1


# ---------------------------------------------------------------------------
# 22.3.2 double click and process restart do not add a second task
# ---------------------------------------------------------------------------


async def test_double_click_and_restart_do_not_add_a_second_task(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    actions, _submitter = build_actions(db, settings, fake_downloader)

    first = await actions.accept(content_id, actor="telegram:111")
    second = await actions.accept(content_id, actor="telegram:111")
    assert first.outcome is AcceptOutcome.SUBMITTED
    assert second.outcome is AcceptOutcome.ALREADY
    assert len(fake_downloader.added) == 1

    # "Restart": new service objects over the same database, then startup
    # reconciliation, which must not resubmit a SUBMITTED job.
    restarted_actions, restarted_submitter = build_actions(db, settings, fake_downloader)
    await reconcile_startup(
        db=db, submitter=restarted_submitter, settings=settings, notifier=LogNotifier()
    )
    third = await restarted_actions.accept(content_id, actor="telegram:111")

    assert third.outcome is AcceptOutcome.ALREADY
    assert len(fake_downloader.added) == 1
    assert await count(db, DownloadJob) == 1
    assert (await job(db, first.job_id or "")).status is S.SUBMITTED


# ---------------------------------------------------------------------------
# 22.3.3 not interested writes the blacklist before deleting messages
# ---------------------------------------------------------------------------


class FailingDeleteBot:
    def __init__(self) -> None:
        self.attempts = 0

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.attempts += 1
        # A transient failure (a permanent one is covered by
        # tests/integration/test_notifications.py: it drops the retry instead).
        raise RuntimeError("timed out")


async def test_not_interested_writes_blacklist_before_deleting_messages(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db)
    async with db.transaction() as session:
        notification = await notification_store.get_or_create(
            session, content_id=content_id, bot_name="subscription", chat_id=111
        )
        await notification_store.record_progress(
            session, notification.id, poster_message_id=10, media_message_ids=[11, 12]
        )

    actions, _ = build_actions(db, settings, fake_downloader)
    result = await actions.block(content_id, actor="telegram:111", chat_id=111)

    # The blacklist row is committed before any deletion is attempted.
    assert result.blocked is True
    assert await count(db, BlacklistEntry) == 1

    bot = FailingDeleteBot()
    deleter = notification_store.MessageDeleter(db=db, bot=bot)
    deleter.enqueue(chat_id=111, ids=result.message_ids, notification_id=result.notification_id)
    report = await deleter.run_once()

    assert bot.attempts == 3  # every message was attempted
    assert report.failed == 1
    # A Telegram failure never removes the user's decision.
    assert await count(db, BlacklistEntry) == 1


# ---------------------------------------------------------------------------
# 22.3.4 a blacklisted code is still rejected by the manual bot
# ---------------------------------------------------------------------------


async def test_blacklisted_code_is_still_rejected_by_the_manual_bot(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    content_id, _ = await seed_content(db, number="5000075")
    actions, submitter = build_actions(db, settings, fake_downloader)
    await actions.block(content_id, actor="telegram:111", chat_id=111)

    manual = ManualIngestService(db=db, gate=ContentGate(), submitter=submitter, settings=settings)
    magnet = f"magnet:?xt=urn:btih:{hex_hash(9001)}&dn=FC2-PPV-5000075"
    result = await manual.ingest(text=magnet, user_id=111)

    assert result.accepted is False
    assert "黑名单" in result.message
    assert len(fake_downloader.added) == 0
    assert await count(db, DownloadJob) == 0


# ---------------------------------------------------------------------------
# 22.3.5 files are organized only after the download completes
# ---------------------------------------------------------------------------


async def test_files_are_organized_only_after_the_download_completes(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    content_id, _release_id = await seed_content(db)
    info_hash = hex_hash(2000 + 567)
    actions, _ = build_actions(db, settings, fake_downloader)
    result = await actions.accept(content_id, actor="telegram:111")
    assert result.job_id is not None

    download_dir = tmp_path / "downloads" / "fc2" / result.job_id / "FC2-PPV-1234567"
    download_dir.mkdir(parents=True)
    (download_dir / "video.mkv").write_bytes(b"x" * 1024)
    monitor = build_monitor(db, settings, fake_downloader)

    # The fake downloader still reports progress 0.0.
    await monitor.poll_once()
    await monitor.poll_once()

    assert (await job(db, result.job_id)).status is S.DOWNLOADING
    library = settings.final_root("fc2") / "FC2-PPV-1234567"
    assert not library.exists()
    assert download_dir.exists()

    # Only once the downloader says complete does the file move.
    fake_downloader.complete(info_hash, download_dir)
    await monitor.poll_once()  # stability observation 1
    await monitor.poll_once()  # stable -> DOWNLOADED
    await monitor.poll_once()  # organize

    assert (library / "video.mkv").exists()
    assert (await job(db, result.job_id)).status is S.ORGANIZED


# ---------------------------------------------------------------------------
# 22.3.6 a failed move never removes the qBittorrent task
# ---------------------------------------------------------------------------


async def test_move_failure_never_removes_the_torrent(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    content_id, _ = await seed_content(db)
    info_hash = hex_hash(2000 + 567)
    actions, _ = build_actions(db, settings, fake_downloader)
    result = await actions.accept(content_id, actor="telegram:111")
    assert result.job_id is not None

    download_dir = tmp_path / "downloads" / "fc2" / result.job_id / "FC2-PPV-1234567"
    download_dir.mkdir(parents=True)
    (download_dir / "video.mkv").write_bytes(b"x" * 1024)
    fake_downloader.complete(info_hash, download_dir)

    monitor = build_monitor(db, settings, fake_downloader)

    def broken_organize(_source: Path, _target: Path) -> None:
        raise OrganizeError("simulated copy failure", stage=_move_stage())

    monitor.organizer.organize = broken_organize  # type: ignore[method-assign]

    for _ in range(3):
        await monitor.poll_once()

    failed = await job(db, result.job_id)
    assert failed.status is S.FAILED
    assert failed.error_code == "organize_failed"
    assert fake_downloader.removed == []  # task untouched
    assert (download_dir / "video.mkv").exists()  # source untouched
    assert info_hash in fake_downloader.torrents


def _move_stage():
    from app.db.enums import MoveStage

    return MoveStage.COPY


# ---------------------------------------------------------------------------
# 22.3.7 task removal always uses deleteFiles=false
# ---------------------------------------------------------------------------


async def test_task_removal_always_uses_delete_files_false(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    content_id, _ = await seed_content(db)
    info_hash = hex_hash(2000 + 567)
    actions, _ = build_actions(db, settings, fake_downloader)
    result = await actions.accept(content_id, actor="telegram:111")
    assert result.job_id is not None

    download_dir = tmp_path / "downloads" / "fc2" / result.job_id / "FC2-PPV-1234567"
    download_dir.mkdir(parents=True)
    (download_dir / "video.mkv").write_bytes(b"x" * 1024)
    fake_downloader.complete(info_hash, download_dir)

    monitor = build_monitor(db, settings, fake_downloader)
    for _ in range(4):
        await monitor.poll_once()

    assert (await job(db, result.job_id)).status is S.TASK_REMOVED
    assert fake_downloader.removed == [(info_hash, False)]
    # The payload stays on disk even though the task is gone.
    library = settings.final_root("fc2") / "FC2-PPV-1234567"
    assert (library / "video.mkv").exists()


# ---------------------------------------------------------------------------
# adapter-level guard for 22.3.7
# ---------------------------------------------------------------------------


async def test_qbittorrent_adapter_refuses_to_delete_files() -> None:
    from app.downloaders.qbittorrent import QBittorrentClient
    from app.schemas.download import DownloadHandle

    client = QBittorrentClient(base_url="http://qb.local:8080", username="u", password="p")
    try:
        try:
            await client.remove(
                DownloadHandle(downloader=DownloaderName.QBITTORRENT, info_hash="a" * 40),
                delete_files=True,
            )
        except ValueError as exc:
            assert "delete_files" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("delete_files=True must be refused")
    finally:
        await client.close()
