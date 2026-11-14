"""Subscription button actions — spec 9.3 / 9.4.

Kept out of the aiogram layer so the transactional behaviour is testable:

* ``accept`` re-checks the gate, writes the accepted entry and the job in one
  transaction, then submits to the downloader. A repeat click returns
  ``already`` with the existing job instead of creating a second one.
* ``block`` writes the blacklist row first (unique key) and only then asks for the
  message group to be deleted — a Telegram failure never rolls back the decision.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import select

from app.config import Settings
from app.db.enums import (
    AcceptedOrigin,
    ContentType,
    DownloaderName,
    DownloadStatus,
    GateReason,
)
from app.db.models import BlacklistEntry, Content, DownloadJob, Notification, Release
from app.db.session import Database
from app.logging import get_logger
from app.services import notifications as notification_store
from app.services.gate import ContentGate, GateConflictError
from app.services.jobs import JobSubmitter
from app.services.state import record_event

_log = get_logger("acceptance")


class AcceptOutcome(StrEnum):
    SUBMITTED = "submitted"
    ALREADY = "already"
    BLACKLISTED = "blacklisted"
    FAILED = "failed"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class AcceptResult:
    outcome: AcceptOutcome
    job_id: str | None = None
    job_status: DownloadStatus | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class BlockResult:
    blocked: bool
    already_blocked: bool = False
    notification_id: str | None = None
    message_ids: list[int] = field(default_factory=list)


class SubscriptionActions:
    def __init__(
        self,
        *,
        db: Database,
        gate: ContentGate,
        submitter: JobSubmitter,
        settings: Settings,
    ) -> None:
        self.db = db
        self.gate = gate
        self.submitter = submitter
        self.settings = settings

    async def accept(self, content_id: str, *, actor: str) -> AcceptResult:
        content, release = await self._load(content_id)
        if content is None or release is None:
            return AcceptResult(AcceptOutcome.MISSING, message="内容不存在")

        category = "fc2" if content.content_type is ContentType.FC2 else "review"
        downloader = DownloaderName(self.settings.downloader_for_category(category))

        try:
            async with self.db.transaction() as session:
                decision = await self.gate.admit_existing(
                    session,
                    content_id=content.id,
                    canonical_key=content.canonical_key,
                    release_id=release.id,
                    info_hash=release.info_hash,
                    origin=AcceptedOrigin.SUBSCRIPTION_BUTTON,
                    category=category,
                    downloader=downloader,
                    actor=actor,
                )
        except GateConflictError:
            return AcceptResult(
                AcceptOutcome.ALREADY,
                job_id=await self._job_for_content(content_id),
                message="已处理",
            )

        if decision.reason is GateReason.BLACKLISTED:
            return AcceptResult(AcceptOutcome.BLACKLISTED, message="该编号已拉黑")

        if decision.job_id is None:
            return AcceptResult(AcceptOutcome.ALREADY, message="已处理")

        if decision.reason is GateReason.ALREADY_ACCEPTED:
            return AcceptResult(
                AcceptOutcome.ALREADY,
                job_id=decision.job_id,
                job_status=await self._job_status(decision.job_id),
                message="已处理",
            )

        status = await self.submitter.submit(decision.job_id)
        if status is DownloadStatus.FAILED:
            return AcceptResult(
                AcceptOutcome.FAILED,
                job_id=decision.job_id,
                job_status=status,
                message="投递失败，可点重试或 /retry",
            )
        return AcceptResult(
            AcceptOutcome.SUBMITTED,
            job_id=decision.job_id,
            job_status=status,
            message="已加入下载队列",
        )

    async def block(self, content_id: str, *, actor: str, chat_id: int) -> BlockResult:
        async with self.db.session() as session:
            content = await session.get(Content, content_id)
            if content is None:
                return BlockResult(blocked=False)
            canonical_key = content.canonical_key

        async with self.db.transaction() as session:
            existing = await session.scalar(
                select(BlacklistEntry.id).where(BlacklistEntry.canonical_key == canonical_key)
            )
            already = existing is not None
            if not already:
                session.add(
                    BlacklistEntry(
                        canonical_key=canonical_key,
                        content_id=content_id,
                        reason="user_rejected",
                        created_by=actor,
                    )
                )
            await record_event(
                session,
                "content.blacklisted",
                content_id=content_id,
                canonical_key=canonical_key,
                actor=actor,
                reason="subscription_button" if not already else "already_blocked",
            )

        async with self.db.session() as session:
            notification = await session.scalar(
                select(Notification).where(
                    Notification.content_id == content_id,
                    Notification.bot_name == "subscription",
                    Notification.chat_id == chat_id,
                )
            )
            ids = notification_store.message_ids(notification) if notification else []
            notification_id = notification.id if notification else None

        _log.info(
            "acceptance.blocked",
            content_id=content_id,
            canonical_key=canonical_key,
            already=already,
            messages=len(ids),
        )
        return BlockResult(
            blocked=True,
            already_blocked=already,
            notification_id=notification_id,
            message_ids=ids,
        )

    # -- helpers ------------------------------------------------------------

    async def _load(self, content_id: str) -> tuple[Content | None, Release | None]:
        async with self.db.session() as session:
            content = await session.get(Content, content_id)
            if content is None:
                return None, None
            release = await session.scalar(
                select(Release)
                .where(Release.content_id == content_id)
                .order_by(Release.published_at.asc().nullslast(), Release.created_at)
                .limit(1)
            )
            return content, release

    async def _job_for_content(self, content_id: str) -> str | None:
        async with self.db.session() as session:
            return await session.scalar(
                select(DownloadJob.id)
                .where(DownloadJob.content_id == content_id)
                .order_by(DownloadJob.created_at.desc())
                .limit(1)
            )

    async def _job_status(self, job_id: str) -> DownloadStatus | None:
        async with self.db.session() as session:
            return await session.scalar(select(DownloadJob.status).where(DownloadJob.id == job_id))
