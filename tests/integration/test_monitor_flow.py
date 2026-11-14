"""Download monitor / organizer / task removal — spec 14.2 / 14.3 / 16.3 / 22.3.

Uses a fake downloader and real files in tmp_path. The invariants under test:

* nothing is organized before the download is complete and stable;
* files are moved before the torrent task is removed;
* removal always uses ``deleteFiles=false``;
* a failed move keeps both the source files and the torrent task.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db.enums import ContentType, DownloaderName, DownloadStatus
from app.db.models import DownloadJob
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.organizer.monitor import DownloadMonitor, reconcile_startup
from app.organizer.mover import OrganizeError, Organizer
from app.organizer.stability import StabilityTracker
from app.services.jobs import JobSubmitter
from app.services.notify import LogNotifier
from sqlalchemy import select

from tests.support import FakeDownloader, create_job, hex_hash

S = DownloadStatus
HASH = hex_hash(301)


def build(db: Database, settings: Settings, fake: FakeDownloader):
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: fake})
    submitter = JobSubmitter(db=db, registry=registry, settings=settings)
    notifier = LogNotifier()
    monitor = DownloadMonitor(
        db=db,
        registry=registry,
        organizer=Organizer(),
        settings=settings,
        stability=StabilityTracker(required_checks=2, interval_seconds=0),
        notifier=notifier,
    )
    return submitter, monitor, notifier


async def prepare_download(
    db: Database,
    settings: Settings,
    fake: FakeDownloader,
    tmp_path: Path,
    *,
    submit: bool = True,
) -> tuple[str, Path]:
    submitter, _monitor, _notifier = build(db, settings, fake)
    _, _, job_id = await create_job(db, info_hash=HASH)
    if submit:
        assert await submitter.submit(job_id) is S.SUBMITTED

    download_dir = tmp_path / "downloads" / "fc2" / job_id / "FC2-PPV-1234567"
    download_dir.mkdir(parents=True)
    (download_dir / "video.mkv").write_bytes(b"x" * 4096)
    (download_dir / "cover.jpg").write_bytes(b"y" * 128)
    return job_id, download_dir


async def job_status(db: Database, job_id: str) -> DownloadJob:
    async with db.session() as session:
        job = await session.get(DownloadJob, job_id)
        assert job is not None
        return job


async def test_full_pipeline_organizes_then_removes(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, _ = build(db, settings, fake_downloader)
    fake_downloader.complete(HASH, download_dir)

    # Tick 1: complete, but stability has only one observation.
    await monitor.poll_once()
    assert (await job_status(db, job_id)).status is S.DOWNLOADING
    library = settings.final_root("fc2") / "FC2-PPV-1234567"
    assert not library.exists()

    # Tick 2: stable -> DOWNLOADED, still nothing moved.
    await monitor.poll_once()
    assert (await job_status(db, job_id)).status is S.DOWNLOADED
    assert not library.exists()

    # Tick 3: organized.
    await monitor.poll_once()
    job = await job_status(db, job_id)
    assert job.status is S.ORGANIZED
    assert job.final_path == str(library)
    assert (library / "video.mkv").read_bytes() == b"x" * 4096
    assert (library / "cover.jpg").exists()
    assert not download_dir.exists()
    # The torrent is still there: removal only happens after organizing.
    assert fake_downloader.removed == []

    # Tick 4: task removed with deleteFiles=false.
    await monitor.poll_once()
    job = await job_status(db, job_id)
    assert job.status is S.TASK_REMOVED
    assert job.removed_at is not None
    assert fake_downloader.removed == [(HASH, False)]
    assert (library / "video.mkv").exists()


async def test_incomplete_download_is_never_organized(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, _ = build(db, settings, fake_downloader)
    # FakeDownloader keeps progress 0.0 / state "downloading".

    await monitor.poll_once()
    await monitor.poll_once()

    job = await job_status(db, job_id)
    assert job.status is S.DOWNLOADING
    assert download_dir.exists()
    assert not (settings.final_root("fc2") / "FC2-PPV-1234567").exists()


async def test_missing_torrent_marks_job_failed_and_keeps_files(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, _ = build(db, settings, fake_downloader)
    fake_downloader.torrents.clear()

    await monitor.poll_once()

    job = await job_status(db, job_id)
    assert job.status is S.FAILED
    assert job.error_code == "downloader_task_missing"
    assert download_dir.exists()


async def test_fatal_downloader_state_marks_job_failed(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, _ = build(db, settings, fake_downloader)
    fake_downloader.fail_state(HASH, "missingFiles")

    await monitor.poll_once()

    job = await job_status(db, job_id)
    assert job.status is S.FAILED
    assert job.error_code == "downloader_state_error"
    assert download_dir.exists()


async def test_move_failure_keeps_source_and_torrent(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, notifier = build(db, settings, fake_downloader)
    fake_downloader.complete(HASH, download_dir)

    def broken_organize(_source: Path, _target: Path) -> None:
        raise OrganizeError(
            "simulated copy failure",
            stage=__import__("app.db.enums", fromlist=["MoveStage"]).MoveStage.COPY,
        )

    monitor.organizer.organize = broken_organize  # type: ignore[method-assign]

    await monitor.poll_once()  # DOWNLOADING
    await monitor.poll_once()  # DOWNLOADED
    await monitor.poll_once()  # organize fails -> FAILED

    job = await job_status(db, job_id)
    assert job.status is S.FAILED
    assert job.error_code == "organize_failed"
    assert download_dir.exists()
    assert (download_dir / "video.mkv").exists()
    assert fake_downloader.removed == []
    assert notifier.messages
    assert "整理失败" in notifier.messages[0]


async def test_removal_failure_keeps_organized_status_and_retries(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, notifier = build(db, settings, fake_downloader)
    fake_downloader.complete(HASH, download_dir)
    fake_downloader.remove_should_fail = True

    await monitor.poll_once()  # DOWNLOADING
    await monitor.poll_once()  # DOWNLOADED
    await monitor.poll_once()  # ORGANIZED
    await monitor.poll_once()  # removal fails, stays ORGANIZED

    job = await job_status(db, job_id)
    assert job.status is S.ORGANIZED
    library = settings.final_root("fc2") / "FC2-PPV-1234567"
    assert (library / "video.mkv").exists()
    assert notifier.messages

    fake_downloader.remove_should_fail = False
    await monitor.poll_once()

    job = await job_status(db, job_id)
    assert job.status is S.TASK_REMOVED
    assert fake_downloader.removed[-1] == (HASH, False)


async def test_startup_reconcile_resubmits_accepted_jobs(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    submitter, _monitor, _notifier = build(db, settings, fake_downloader)
    _, _, job_id = await create_job(db, info_hash=hex_hash(302))

    summary = await reconcile_startup(db=db, submitter=submitter, settings=settings)

    assert summary["accepted_resubmitted"] == 1
    job = await job_status(db, job_id)
    assert job.status is S.SUBMITTED
    assert len(fake_downloader.added) == 1

    # Running it again is idempotent.
    summary = await reconcile_startup(db=db, submitter=submitter, settings=settings)
    assert summary["accepted_resubmitted"] == 0
    assert len(fake_downloader.added) == 1


async def test_organize_uses_category_root(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    submitter, _monitor, _notifier = build(db, settings, fake_downloader)
    hash_jav = hex_hash(303)
    _, _, job_id = await create_job(
        db,
        info_hash=hash_jav,
        category="jav",
        canonical_key=f"infohash:{hash_jav}",
        content_type=ContentType.GENERIC,
        display_code=None,
        raw_title="ABC-123",
    )
    assert await submitter.submit(job_id) is S.SUBMITTED

    download_dir = tmp_path / "downloads" / "jav" / job_id / "ABC-123"
    download_dir.mkdir(parents=True)
    (download_dir / "movie.mkv").write_bytes(b"z" * 64)
    fake_downloader.complete(hash_jav, download_dir)

    _, monitor, _ = build(db, settings, fake_downloader)
    for _ in range(4):
        await monitor.poll_once()

    job = await job_status(db, job_id)
    assert job.status is S.TASK_REMOVED
    assert (settings.final_root("jav") / "ABC-123" / "movie.mkv").exists()


async def test_decision_events_record_each_transition(
    db: Database, settings: Settings, fake_downloader: FakeDownloader, tmp_path: Path
) -> None:
    job_id, download_dir = await prepare_download(db, settings, fake_downloader, tmp_path)
    _, monitor, _ = build(db, settings, fake_downloader)
    fake_downloader.complete(HASH, download_dir)
    for _ in range(4):
        await monitor.poll_once()

    from app.db.models import DecisionEvent

    async with db.session() as session:
        events = list(
            (
                await session.scalars(
                    select(DecisionEvent)
                    .where(DecisionEvent.job_id == job_id)
                    .order_by(DecisionEvent.created_at)
                )
            ).all()
        )
    statuses = [(event.from_status, event.to_status) for event in events]
    assert ("ACCEPTED", "SUBMITTED") in statuses
    assert ("SUBMITTED", "DOWNLOADING") in statuses
    assert ("DOWNLOADING", "DOWNLOADED") in statuses
    assert ("DOWNLOADED", "ORGANIZING") in statuses
    assert ("ORGANIZING", "ORGANIZED") in statuses
    assert ("ORGANIZED", "TASK_REMOVED") in statuses
