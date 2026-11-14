"""Download completion monitor and organizer driver — spec 13 / 14.2 / 14.3.

Per tick, for every job in an actionable state:

* ``SUBMITTED``/``DOWNLOADING``: ask the downloader, apply the completion rules,
  then require two identical filesystem observations before moving on.
* ``DOWNLOADED``/``ORGANIZING``: organize (idempotent, so a crash mid-organize
  is retried), then mark ``ORGANIZED``.
* ``ORGANIZED``: remove the torrent task with ``deleteFiles=false`` and only
  then mark ``TASK_REMOVED``.

Nothing is moved before the download is complete; nothing is deleted before the
organizer reports success. Every state change is a conditional update, so a
second worker (or a restarted container) cannot advance the same job twice.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, select

from app.config import Settings
from app.db.enums import ContentType, DownloaderName, DownloadStatus
from app.db.models import Content, DownloadJob, Release
from app.db.session import Database
from app.downloaders.base import DownloaderError
from app.downloaders.coordinator import DownloaderRegistry
from app.logging import get_logger
from app.organizer.mover import OrganizeError, Organizer
from app.organizer.paths import PathMapper, PathMappingError, build_directory
from app.organizer.stability import StabilityTracker
from app.schemas.download import DownloadHandle, evaluate_completion
from app.services.notify import AdminNotifier
from app.services.state import (
    IN_FLIGHT,
    ORGANIZE_PENDING,
    StaleTransitionError,
    transition_job,
)

_log = get_logger("monitor")

S = DownloadStatus

MAX_JOBS_PER_TICK = 200


@dataclass(slots=True)
class MonitorReport:
    polled: int = 0
    transitioned: int = 0
    organized: int = 0
    removed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class JobSnapshotView:
    id: str
    status: DownloadStatus
    downloader: DownloaderName
    category: str
    info_hash: str | None
    downloader_task_id: str | None
    remote_content_path: str | None
    local_content_path: str | None
    final_path: str | None
    content_type: ContentType
    content_display_code: str | None
    content_canonical_key: str
    release_raw_title: str | None


class DownloadMonitor:
    def __init__(
        self,
        *,
        db: Database,
        registry: DownloaderRegistry,
        organizer: Organizer,
        settings: Settings,
        stability: StabilityTracker | None = None,
        notifier: AdminNotifier | None = None,
        mapper: PathMapper | None = None,
    ) -> None:
        self.db = db
        self.registry = registry
        self.organizer = organizer
        self.settings = settings
        self.mapper = mapper or PathMapper(settings.storage.path_mappings)
        qb = settings.downloaders.qbittorrent
        self.stability = stability or StabilityTracker(
            required_checks=qb.stability_checks,
            interval_seconds=qb.stability_interval_seconds,
            max_wait_seconds=qb.max_stability_wait_seconds,
        )
        self.notifier = notifier
        self._locks: dict[str, asyncio.Lock] = {}

    # -- tick ---------------------------------------------------------------

    async def poll_once(self) -> MonitorReport:
        job_ids = await self._pending_job_ids()
        report = MonitorReport(polled=len(job_ids))
        for job_id in job_ids:
            lock = self._locks.setdefault(job_id, asyncio.Lock())
            if lock.locked():
                report.skipped += 1
                continue
            async with lock:
                try:
                    await self._process(job_id, report)
                except StaleTransitionError:
                    report.skipped += 1
                    _log.info("monitor.stale", job_id=job_id)
                except Exception as exc:
                    report.errors.append(f"{job_id}: {type(exc).__name__}: {exc}")
                    _log.exception("monitor.job_error", job_id=job_id)
        self._prune_locks()
        if report.polled:
            _log.info(
                "monitor.tick",
                polled=report.polled,
                transitioned=report.transitioned,
                organized=report.organized,
                removed=report.removed,
                failed=report.failed,
                skipped=report.skipped,
                error_count=len(report.errors),
            )
        return report

    async def _pending_job_ids(self) -> list[str]:
        statuses = list(IN_FLIGHT | ORGANIZE_PENDING | {S.ORGANIZED})
        stmt = (
            select(DownloadJob.id)
            .where(DownloadJob.status.in_(statuses))
            .order_by(DownloadJob.created_at)
            .limit(MAX_JOBS_PER_TICK)
        )
        async with self.db.session() as session:
            return list((await session.scalars(stmt)).all())

    def _prune_locks(self) -> None:
        for job_id in [key for key, lock in self._locks.items() if not lock.locked()]:
            self._locks.pop(job_id, None)

    # -- job dispatch -------------------------------------------------------

    async def _process(self, job_id: str, report: MonitorReport) -> None:
        view = await self._load_view(job_id)
        if view is None:
            return
        if view.status in IN_FLIGHT:
            await self._advance_download(view, report)
        elif view.status in ORGANIZE_PENDING:
            await self._advance_organize(view, report)
        elif view.status is S.ORGANIZED:
            await self._advance_removal(view, report)

    async def _advance_download(self, view: JobSnapshotView, report: MonitorReport) -> None:
        if not view.info_hash:
            await self._fail(view, "missing_info_hash", "job has no info hash", report)
            return

        downloader = self.registry.get(view.downloader)
        handle = DownloadHandle(
            downloader=view.downloader,
            info_hash=view.info_hash,
            task_id=view.downloader_task_id,
        )
        try:
            snapshot = await downloader.get(handle)
        except DownloaderError as exc:
            # Transient: the downloader is down or restarting. Keep the job and
            # retry on the next tick; never mark a network blip as failure.
            _log.warning(
                "monitor.downloader_unavailable",
                job_id=view.id,
                downloader=view.downloader.value,
                error_code=exc.code,
            )
            report.errors.append(f"{view.id}: {exc.code}")
            return

        if snapshot is None:
            await self._fail(
                view,
                "downloader_task_missing",
                "torrent is not present in the downloader",
                report,
            )
            return

        verdict = evaluate_completion(snapshot)
        if verdict.fatal:
            await self._fail(view, "downloader_state_error", verdict.reason, report)
            return

        remote_path = snapshot.content_path or snapshot.save_path
        current = view.status
        if current is S.SUBMITTED:
            await self._transition(
                view,
                S.DOWNLOADING,
                expected=S.SUBMITTED,
                extra_values={
                    "remote_content_path": remote_path,
                    "remote_save_path": snapshot.save_path,
                },
            )
            current = S.DOWNLOADING
            report.transitioned += 1

        if not verdict.complete:
            return

        local_path = self._local_content_path(view, remote_path)
        if local_path is None:
            await self._fail(
                view,
                "content_path_unmapped",
                f"cannot map downloader path {remote_path!r} to a container path",
                report,
                expected=current,
            )
            return

        try:
            stability = self.stability.observe(view.id, local_path)
        except FileNotFoundError:
            await self._fail(
                view,
                "content_path_missing",
                f"completed download path is missing: {local_path}",
                report,
                expected=current,
            )
            return
        except OSError as exc:
            await self._fail(
                view,
                "content_path_unreadable",
                f"cannot read {local_path}: {exc}",
                report,
                expected=current,
            )
            return

        if stability.timed_out:
            await self._fail(
                view,
                "stability_timeout",
                f"files kept changing for {stability.waited_seconds:.0f}s",
                report,
                expected=current,
            )
            return
        if not stability.stable:
            _log.info(
                "monitor.waiting_stable",
                job_id=view.id,
                reason=stability.reason,
                file_count=stability.signature.file_count,
                bytes=stability.signature.total_bytes,
            )
            return

        await self._transition(
            view,
            S.DOWNLOADED,
            expected=current,
            extra_values={
                "remote_content_path": remote_path,
                "local_content_path": str(local_path),
            },
        )
        report.transitioned += 1
        _log.info(
            "monitor.downloaded",
            job_id=view.id,
            path=str(local_path),
            files=stability.signature.file_count,
            bytes=stability.signature.total_bytes,
        )

    async def _advance_organize(self, view: JobSnapshotView, report: MonitorReport) -> None:
        current = view.status
        if current is S.DOWNLOADED:
            await self._transition(view, S.ORGANIZING, expected=S.DOWNLOADED)
            current = S.ORGANIZING
            report.transitioned += 1

        local_path = self._existing_local_path(view)
        if local_path is None:
            await self._fail(
                view,
                "content_path_missing",
                f"download path is gone: {view.local_content_path or view.remote_content_path}",
                report,
                expected=current,
            )
            return

        target_name = self._target_name(view)
        try:
            target_dir = build_directory(
                self.settings.final_root(view.category),
                target_name,
                max_length=self.settings.storage.dir_name_max_length,
            )
        except Exception as exc:
            await self._fail(view, "target_path_invalid", str(exc), report, expected=current)
            return

        try:
            result = self.organizer.organize(local_path, target_dir)
        except OrganizeError as exc:
            # Source files and the torrent task stay exactly where they are.
            await self._fail(view, "organize_failed", exc.describe(), report, expected=current)
            await self._notify(f"整理失败 job={view.id}\n{exc.describe()}\n源文件与种子任务已保留")
            return

        await self._transition(
            view,
            S.ORGANIZED,
            expected=current,
            extra_values={"final_path": str(result.target_dir)},
        )
        self.stability.forget(view.id)
        report.organized += 1
        _log.info(
            "monitor.organized",
            job_id=view.id,
            final_path=str(result.target_dir),
            method=result.method,
            files=result.file_count,
            bytes=result.total_bytes,
        )

    async def _advance_removal(self, view: JobSnapshotView, report: MonitorReport) -> None:
        if view.info_hash:
            downloader = self.registry.get(view.downloader)
            handle = DownloadHandle(
                downloader=view.downloader,
                info_hash=view.info_hash,
                task_id=view.downloader_task_id,
            )
            try:
                # deleteFiles=false is enforced inside the adapter as well.
                await downloader.remove(handle, delete_files=False)
            except DownloaderError as exc:
                # Files are already organized: keep ORGANIZED and retry later.
                _log.error(
                    "monitor.remove_failed",
                    job_id=view.id,
                    downloader=view.downloader.value,
                    error_code=exc.code,
                )
                await self._notify(
                    f"任务清理失败 job={view.id}：{exc.code}\n媒体文件已整理，任务保留待重试"
                )
                return

        await self._transition(view, S.TASK_REMOVED, expected=S.ORGANIZED)
        report.removed += 1
        _log.info("monitor.task_removed", job_id=view.id, delete_files=False)

    # -- helpers ------------------------------------------------------------

    async def _load_view(self, job_id: str) -> JobSnapshotView | None:
        stmt = (
            select(DownloadJob, Content, Release)
            .join(Content, DownloadJob.content_id == Content.id)
            .join(Release, DownloadJob.release_id == Release.id)
            .where(DownloadJob.id == job_id)
        )
        async with self.db.session() as session:
            row = (await session.execute(stmt)).first()
        if row is None:
            return None
        job, content, release = row
        return JobSnapshotView(
            id=job.id,
            status=job.status,
            downloader=job.downloader,
            category=job.category,
            info_hash=job.info_hash,
            downloader_task_id=job.downloader_task_id,
            remote_content_path=job.remote_content_path,
            local_content_path=job.local_content_path,
            final_path=job.final_path,
            content_type=content.content_type,
            content_display_code=content.display_code,
            content_canonical_key=content.canonical_key,
            release_raw_title=release.raw_title,
        )

    def _local_content_path(self, view: JobSnapshotView, remote_path: str | None) -> Path | None:
        if view.local_content_path:
            return Path(view.local_content_path)
        if not remote_path:
            return None
        try:
            return self.mapper.to_local(remote_path)
        except PathMappingError as exc:
            _log.error("monitor.path_mapping_failed", job_id=view.id, detail=str(exc))
            return None

    def _existing_local_path(self, view: JobSnapshotView) -> Path | None:
        path = self._local_content_path(view, view.remote_content_path)
        if path is None or not path.exists():
            return None
        return path

    def _target_name(self, view: JobSnapshotView) -> str:
        if view.content_type is ContentType.FC2:
            return view.content_display_code or view.content_canonical_key
        return view.release_raw_title or view.info_hash or view.content_canonical_key

    async def _transition(
        self,
        view: JobSnapshotView,
        target: DownloadStatus,
        *,
        expected: DownloadStatus,
        extra_values: dict[str, object] | None = None,
    ) -> None:
        async with self.db.transaction() as session:
            await transition_job(
                session,
                view.id,
                target,
                expected_status=expected,
                extra_values=extra_values,
                actor="monitor",
            )

    async def _fail(
        self,
        view: JobSnapshotView,
        code: str,
        message: str,
        report: MonitorReport,
        *,
        expected: DownloadStatus | None = None,
    ) -> None:
        try:
            async with self.db.transaction() as session:
                await transition_job(
                    session,
                    view.id,
                    S.FAILED,
                    expected_status=expected or view.status,
                    error_code=code,
                    error_message=message[:2000],
                    actor="monitor",
                )
        except StaleTransitionError:
            report.skipped += 1
            return
        self.stability.forget(view.id)
        report.failed += 1
        _log.error("monitor.failed", job_id=view.id, error_code=code, detail=message)

    async def _notify(self, text: str) -> None:
        if self.notifier is not None:
            await self.notifier.notify(text)


async def reconcile_startup(
    *,
    db: Database,
    submitter: object,
    settings: Settings,
) -> dict[str, int]:
    """Scan unfinished jobs at container start (spec 13).

    ``ACCEPTED`` jobs are (re)submitted — this covers a crash between the gate
    transaction and the downloader call. Everything else is left to the monitor,
    which reconciles each job against qBittorrent on the next tick.
    """

    summary = {
        "accepted_resubmitted": 0,
        "in_flight": 0,
        "organize_pending": 0,
        "organized_waiting_removal": 0,
        "failed": 0,
    }

    async with db.session() as session:
        accepted_ids = list(
            (
                await session.scalars(
                    select(DownloadJob.id).where(DownloadJob.status == S.ACCEPTED)
                )
            ).all()
        )
        rows = (
            await session.execute(
                select(DownloadJob.status, func.count()).group_by(DownloadJob.status)
            )
        ).all()

    for status_value, count in rows:
        if status_value in IN_FLIGHT:
            summary["in_flight"] += int(count)
        elif status_value in ORGANIZE_PENDING:
            summary["organize_pending"] += int(count)
        elif status_value is S.ORGANIZED:
            summary["organized_waiting_removal"] += int(count)
        elif status_value is S.FAILED:
            summary["failed"] += int(count)

    submit = getattr(submitter, "submit", None)
    if callable(submit):
        for job_id in accepted_ids:
            await submit(job_id)
            summary["accepted_resubmitted"] += 1

    _log.info(
        "monitor.reconcile",
        **summary,
        downloader_default=settings.download.default_downloader,
    )
    return summary


async def run_monitor_loop(
    monitor: DownloadMonitor,
    *,
    interval_seconds: float,
    stop_event: asyncio.Event,
) -> None:
    """Poll until shutdown; a failing tick is logged, never fatal (spec 20)."""

    while not stop_event.is_set():
        try:
            await monitor.poll_once()
        except Exception:
            _log.exception("monitor.tick_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
