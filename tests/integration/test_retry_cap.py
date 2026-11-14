"""Bounded automatic resubmission — spec 11.3 / 13.

A job that cannot be submitted must not be retried forever: after
``download.max_retries`` attempts it is marked FAILED and the administrator is
told, while the accepted entry (and therefore the "never push again" rule) stays.
"""

from __future__ import annotations

from app.config import Settings
from app.db.enums import DownloaderName, DownloadStatus
from app.db.models import DownloadJob
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.organizer.monitor import reconcile_startup
from app.services.jobs import JobSubmitter
from app.services.notify import LogNotifier
from sqlalchemy import update

from tests.support import FakeDownloader, create_job, hex_hash

S = DownloadStatus


def build(db: Database, settings: Settings, fake: FakeDownloader):
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: fake})
    return JobSubmitter(db=db, registry=registry, settings=settings), LogNotifier()


async def set_retry_count(db: Database, job_id: str, value: int) -> None:
    async with db.transaction() as session:
        await session.execute(
            update(DownloadJob).where(DownloadJob.id == job_id).values(retry_count=value)
        )


async def job(db: Database, job_id: str) -> DownloadJob:
    async with db.session() as session:
        loaded = await session.get(DownloadJob, job_id)
        assert loaded is not None
        return loaded


async def test_reconcile_resubmits_and_counts_the_attempt(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    submitter, notifier = build(db, settings, fake_downloader)
    _, _, job_id = await create_job(db, info_hash=hex_hash(701))
    await set_retry_count(db, job_id, settings.download.max_retries - 1)

    summary = await reconcile_startup(
        db=db, submitter=submitter, settings=settings, notifier=notifier
    )

    assert summary["accepted_resubmitted"] == 1
    assert summary["accepted_abandoned"] == 0
    assert (await job(db, job_id)).status is S.SUBMITTED
    assert (await job(db, job_id)).retry_count == settings.download.max_retries


async def test_reconcile_abandons_a_job_that_hit_the_limit(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    submitter, notifier = build(db, settings, fake_downloader)
    _, _, job_id = await create_job(db, info_hash=hex_hash(702))
    await set_retry_count(db, job_id, settings.download.max_retries)

    summary = await reconcile_startup(
        db=db, submitter=submitter, settings=settings, notifier=notifier
    )

    assert summary["accepted_abandoned"] == 1
    assert summary["accepted_resubmitted"] == 0
    failed = await job(db, job_id)
    assert failed.status is S.FAILED
    assert failed.error_code == "max_retries_exceeded"
    assert fake_downloader.added == []
    assert notifier.messages and "FAILED" in notifier.messages[0]

    # A second start must not resurrect it: FAILED jobs are not auto-retried.
    await reconcile_startup(db=db, submitter=submitter, settings=settings, notifier=notifier)
    assert fake_downloader.added == []
    assert (await job(db, job_id)).status is S.FAILED
