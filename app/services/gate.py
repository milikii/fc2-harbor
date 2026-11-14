"""Unified content gate — spec 11.

The gate is the single place that decides whether a content may be pushed or
turned into a download. Both bots and (later) the Nyaa poller use it, and no
source adapter may bypass it.

Admission order is fixed by spec 11.1:

1. ``canonical_key`` in ``blacklist_entries``
2. ``canonical_key`` in ``accepted_entries``
3. ``info_hash`` present in active/completed ``download_jobs``
4. ``(source, source_item_id)`` already processed

Blacklist wins over everything. The check and the ``accepted_entries`` write
must happen in one transaction (spec 11.2); :meth:`ContentGate.admit` therefore
must be called inside ``Database.transaction()`` and never commits by itself.

Contract for callers: **evaluate the gate before writing the release row.**
``admit`` writes content + release + accepted entry + job atomically, so step 4
("this source item was already processed") stays meaningful for the next caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

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
    DownloadJob,
    Release,
)
from app.logging import get_logger
from app.services.state import ACTIVE_OR_COMPLETED, record_event

_log = get_logger("gate")


class GateConflictError(RuntimeError):
    """A concurrent writer won the race; the caller must re-evaluate."""


@dataclass(frozen=True, slots=True)
class GateSubject:
    """Everything the gate needs, independent of the source."""

    content_type: ContentType
    canonical_key: str
    source: str
    source_item_id: str
    display_code: str | None = None
    info_hash: str | None = None
    source_url: str | None = None
    magnet: str | None = None
    raw_title: str | None = None
    published_at: datetime | None = None
    raw_payload: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason: GateReason
    content_id: str | None = None
    release_id: str | None = None
    job_id: str | None = None
    existing_job_id: str | None = None
    detail: str | None = None

    @property
    def rejected(self) -> bool:
        return not self.allowed


class ContentGate:
    """Stateless gate; safe to share across tasks."""

    async def evaluate(self, session: AsyncSession, subject: GateSubject) -> GateDecision:
        """Run the four checks in order without writing anything."""

        # 1. Blacklist — highest priority (spec 11.1).
        blacklisted = await session.scalar(
            select(BlacklistEntry.id).where(BlacklistEntry.canonical_key == subject.canonical_key)
        )
        if blacklisted is not None:
            return self._reject(subject, GateReason.BLACKLISTED)

        # 2. Already accepted (button click, manual submission, admin).
        accepted = await session.scalar(
            select(AcceptedEntry.id).where(AcceptedEntry.canonical_key == subject.canonical_key)
        )
        if accepted is not None:
            return self._reject(subject, GateReason.ALREADY_ACCEPTED)

        # 3. Info hash already owned by an active or completed job.
        if subject.info_hash:
            existing_job = await session.scalar(
                select(DownloadJob.id).where(
                    DownloadJob.info_hash == subject.info_hash,
                    DownloadJob.status.in_(list(ACTIVE_OR_COMPLETED)),
                )
            )
            if existing_job is not None:
                return self._reject(
                    subject, GateReason.INFO_HASH_IN_JOBS, existing_job_id=existing_job
                )

        # 4. Same source item already ingested.
        processed = await session.scalar(
            select(Release.id).where(
                Release.source == subject.source,
                Release.source_item_id == subject.source_item_id,
            )
        )
        if processed is not None:
            return self._reject(subject, GateReason.SOURCE_ITEM_ALREADY_PROCESSED)

        return GateDecision(allowed=True, reason=GateReason.ALLOWED)

    async def admit(
        self,
        session: AsyncSession,
        subject: GateSubject,
        *,
        origin: AcceptedOrigin,
        category: str,
        downloader: DownloaderName,
        actor: str | None = None,
    ) -> GateDecision:
        """Evaluate and, if allowed, persist content/release/accepted/job.

        Must run inside a transaction owned by the caller. Raises
        :class:`GateConflictError` if the unique constraints fire because another
        task admitted the same subject first; the caller rolls back and reports
        "already processed".
        """

        decision = await self.evaluate(session, subject)
        if decision.rejected:
            await record_event(
                session,
                "gate.rejected",
                canonical_key=subject.canonical_key,
                release_id=None,
                actor=actor,
                reason=decision.reason.value,
            )
            _log.info(
                "gate.rejected",
                canonical_key=subject.canonical_key,
                source=subject.source,
                reason=decision.reason.value,
            )
            return decision

        content = await self._get_or_create_content(session, subject)
        release = Release(
            content_id=content.id,
            source=subject.source,
            source_item_id=subject.source_item_id,
            source_url=subject.source_url,
            magnet=subject.magnet,
            info_hash=subject.info_hash,
            raw_title=subject.raw_title,
            published_at=subject.published_at,
            raw_payload_json=subject.raw_payload,
        )
        try:
            session.add(release)
            # Flush so the generated UUID is available for the job's FK.
            await session.flush()
            session.add(
                AcceptedEntry(
                    canonical_key=subject.canonical_key,
                    content_id=content.id,
                    origin=origin,
                )
            )
            job = DownloadJob(
                release_id=release.id,
                content_id=content.id,
                downloader=downloader,
                info_hash=subject.info_hash,
                status=DownloadStatus.ACCEPTED,
                category=category,
                idempotency_key=f"download:{release.id}",
            )
            session.add(job)
            await session.flush()
        except IntegrityError as exc:
            raise GateConflictError(
                f"concurrent admission for {subject.canonical_key} "
                f"({subject.source}:{subject.source_item_id})"
            ) from exc

        await record_event(
            session,
            "gate.accepted",
            canonical_key=subject.canonical_key,
            content_id=content.id,
            release_id=release.id,
            job_id=job.id,
            actor=actor,
            reason=origin.value,
        )
        _log.info(
            "gate.accepted",
            canonical_key=subject.canonical_key,
            content_id=content.id,
            release_id=release.id,
            job_id=job.id,
            source=subject.source,
            status=job.status.value,
        )
        return GateDecision(
            allowed=True,
            reason=GateReason.ALLOWED,
            content_id=content.id,
            release_id=release.id,
            job_id=job.id,
        )

    async def _get_or_create_content(self, session: AsyncSession, subject: GateSubject) -> Content:
        content = await session.scalar(
            select(Content).where(Content.canonical_key == subject.canonical_key)
        )
        if content is not None:
            if not content.display_code and subject.display_code:
                content.display_code = subject.display_code
            if not content.title_ja and subject.raw_title:
                # The release title is a fallback label only; provider metadata
                # overwrites it in M3.
                content.title_ja = subject.raw_title
            return content

        content = Content(
            content_type=subject.content_type,
            canonical_key=subject.canonical_key,
            display_code=subject.display_code,
            title_ja=subject.raw_title,
        )
        session.add(content)
        await session.flush()
        return content

    @staticmethod
    def _reject(
        subject: GateSubject,
        reason: GateReason,
        *,
        existing_job_id: str | None = None,
    ) -> GateDecision:
        detail = f"{subject.canonical_key} rejected: {reason.value}"
        _log.info(
            "gate.rejected",
            canonical_key=subject.canonical_key,
            source=subject.source,
            reason=reason.value,
            job_id=existing_job_id,
        )
        return GateDecision(
            allowed=False,
            reason=reason,
            existing_job_id=existing_job_id,
            detail=detail,
        )
