"""Job submission to the downloader — spec 9.3 / 10 / 14.1.

The transaction boundary is deliberately split around the network call:

1. read the job (no transaction held),
2. call the downloader (idempotent: an existing hash counts as success),
3. record task id / paths and move ``ACCEPTED -> SUBMITTED`` with one
   conditional update.

If the process dies between 2 and 3, the job stays ``ACCEPTED`` and startup
reconciliation re-runs step 2; qBittorrent already knows the hash, so nothing is
added twice. A failed add marks the job ``FAILED`` but keeps the
``accepted_entries`` row (spec 11.3) — the content is never pushed again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from app.config import Settings
from app.db.enums import DownloaderName, DownloadStatus
from app.db.models import DownloadJob, Release
from app.db.session import Database
from app.downloaders.base import DownloaderError
from app.downloaders.coordinator import DownloaderRegistry
from app.logging import get_logger
from app.schemas.download import DownloadRequest
from app.services.state import StaleTransitionError, transition_job

_log = get_logger("jobs")

S = DownloadStatus

STAGE_SUBMIT = "submit"
STAGE_DOWNLOAD = "download"
STAGE_ORGANIZE = "organize"
STAGE_REMOVE = "remove"


class JobNotFoundError(RuntimeError):
    """No download job with that id."""


@dataclass(frozen=True, slots=True)
class JobView:
    id: str
    status: DownloadStatus
    downloader: DownloaderName
    category: str
    info_hash: str | None
    downloader_task_id: str | None
    magnet: str | None
    release_info_hash: str | None
    submitted_at: datetime | None
    completed_at: datetime | None
    organized_at: datetime | None
    retry_count: int = 0


def job_stage(view: JobView) -> str:
    """Which stage a job is in, derived from its timestamps (spec 13)."""

    if view.organized_at is not None:
        return STAGE_REMOVE
    if view.completed_at is not None:
        return STAGE_ORGANIZE
    if view.submitted_at is not None:
        return STAGE_DOWNLOAD
    return STAGE_SUBMIT


class JobSubmitter:
    """Adds accepted jobs to the configured downloader."""

    def __init__(self, *, db: Database, registry: DownloaderRegistry, settings: Settings) -> None:
        self.db = db
        self.registry = registry
        self.settings = settings

    async def load(self, job_id: str) -> JobView | None:
        stmt = (
            select(DownloadJob, Release)
            .join(Release, DownloadJob.release_id == Release.id)
            .where(DownloadJob.id == job_id)
        )
        async with self.db.session() as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            return None
        job, release = row
        return JobView(
            id=job.id,
            status=job.status,
            downloader=job.downloader,
            category=job.category,
            info_hash=job.info_hash,
            downloader_task_id=job.downloader_task_id,
            magnet=release.magnet,
            release_info_hash=release.info_hash,
            submitted_at=job.submitted_at,
            completed_at=job.completed_at,
            organized_at=job.organized_at,
            retry_count=job.retry_count,
        )

    def save_path_for(
        self, category: str, job_id: str, downloader: DownloaderName | None = None
    ) -> str:
        """Per-downloader save path (spec 14.1 / 15)."""

        if downloader is DownloaderName.TRANSMISSION:
            template = self.settings.downloaders.transmission.save_path_template
        else:
            template = self.settings.downloaders.qbittorrent.save_path_template
        return template.format(category=category, job_id=job_id)

    def tag_for(self, job_id: str) -> str:
        return f"{self.settings.downloaders.qbittorrent.tag_prefix}-{job_id}"

    async def submit(
        self,
        job_id: str,
        *,
        allow_retry: bool = False,
        count_attempt: bool = False,
    ) -> DownloadStatus:
        view = await self.load(job_id)
        if view is None:
            raise JobNotFoundError(f"download job {job_id} not found")

        if view.status in {
            S.SUBMITTED,
            S.DOWNLOADING,
            S.DOWNLOADED,
            S.ORGANIZING,
            S.ORGANIZED,
            S.TASK_REMOVED,
        }:
            # Already submitted (double click, restart, retry): no side effect.
            return view.status
        if view.status in {S.BLOCKED, S.CANCELLED}:
            return view.status
        if view.status is S.FAILED and not allow_retry:
            return view.status

        magnet = view.magnet
        info_hash = view.info_hash or view.release_info_hash
        if not magnet or not info_hash:
            await self._mark_failed(view, "missing_magnet", "release has no magnet or info hash")
            return S.FAILED

        downloader = self.registry.get(view.downloader)
        save_path = self.save_path_for(view.category, view.id, view.downloader)
        request = DownloadRequest(
            magnet=magnet,
            info_hash=info_hash,
            save_path=save_path,
            category=self.settings.downloaders.qbittorrent.category,
            tag=self.tag_for(view.id),
            job_id=view.id,
        )

        try:
            handle = await downloader.add(request)
        except DownloaderError as exc:
            _log.error(
                "job.submit_failed",
                job_id=view.id,
                downloader=view.downloader.value,
                error_code=exc.code,
                detail=str(exc),
            )
            await self._mark_failed(view, exc.code, str(exc))
            return S.FAILED

        try:
            async with self.db.transaction() as session:
                await transition_job(
                    session,
                    view.id,
                    S.SUBMITTED,
                    expected_status=view.status,
                    extra_values={
                        "downloader_task_id": handle.task_id or handle.info_hash,
                        "info_hash": handle.info_hash,
                        "remote_save_path": save_path,
                    },
                    actor="system",
                    reason="submitted to downloader",
                    increment_retry=count_attempt,
                )
        except StaleTransitionError:
            # Another task advanced the job while we were talking to qB. The
            # torrent exists once, which is what matters; log and move on.
            _log.warning("job.submit_stale", job_id=view.id)
            return await self._current_status(view.id)

        _log.info(
            "job.submitted",
            job_id=view.id,
            downloader=view.downloader.value,
            info_hash=handle.info_hash,
            save_path=save_path,
        )
        return S.SUBMITTED

    async def retry(self, job_id: str) -> DownloadStatus:
        """Retry the *original* job at the stage it failed at (spec 11.3)."""

        view = await self.load(job_id)
        if view is None:
            raise JobNotFoundError(f"download job {job_id} not found")
        if view.status is not S.FAILED:
            return view.status

        target = {
            STAGE_SUBMIT: S.ACCEPTED,
            STAGE_DOWNLOAD: S.DOWNLOADING,
            STAGE_ORGANIZE: S.ORGANIZING,
            STAGE_REMOVE: S.ORGANIZED,
        }[job_stage(view)]

        try:
            async with self.db.transaction() as session:
                await transition_job(
                    session,
                    view.id,
                    target,
                    expected_status=S.FAILED,
                    increment_retry=True,
                    actor="admin",
                    reason="manual retry",
                )
        except StaleTransitionError:
            return await self._current_status(view.id)

        if target is S.ACCEPTED:
            return await self.submit(view.id, allow_retry=True)
        return target

    async def _mark_failed(self, view: JobView, code: str, message: str) -> None:
        try:
            async with self.db.transaction() as session:
                await transition_job(
                    session,
                    view.id,
                    S.FAILED,
                    expected_status=view.status,
                    error_code=code,
                    error_message=message[:2000],
                    actor="system",
                )
        except StaleTransitionError:
            _log.warning("job.fail_stale", job_id=view.id, error_code=code)

    async def _current_status(self, job_id: str) -> DownloadStatus:
        async with self.db.session() as session:
            status = await session.scalar(
                select(DownloadJob.status).where(DownloadJob.id == job_id)
            )
        return status or S.FAILED
