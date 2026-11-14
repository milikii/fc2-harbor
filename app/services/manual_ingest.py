"""Manual magnet ingestion — spec 10.

The manual bot is a thin shell around this service: parse the magnet, normalise
the FC2 number, ask the *same* content gate as the subscription flow, then
create the job and submit it to the downloader.

Category handling (spec 10.8): an FC2 number implies ``fc2``. Anything else is
ambiguous, so the service returns ``needs_category=True`` and the bot shows the
category buttons; nothing is written until the user confirms, and the confirmed
category goes through the same gate.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import CATEGORIES, Settings
from app.db.enums import (
    AcceptedOrigin,
    ContentType,
    DownloaderName,
    DownloadStatus,
    GateReason,
)
from app.db.session import Database
from app.logging import get_logger
from app.schemas.release import MagnetInfo
from app.services.gate import ContentGate, GateConflictError, GateSubject
from app.services.jobs import JobSubmitter
from app.services.normalize import (
    MagnetError,
    normalize_candidate,
    normalize_fc2,
    parse_magnet,
)

_log = get_logger("manual")

CATEGORY_LABELS: dict[str, str] = {
    "fc2": "FC2",
    "jav": "JAV",
    "cn": "国产",
    "western": "欧美",
    "review": "待确认",
}


@dataclass(frozen=True, slots=True)
class IngestResult:
    accepted: bool
    reason: GateReason
    message: str
    category: str | None = None
    job_id: str | None = None
    content_id: str | None = None
    display_code: str | None = None
    needs_category: bool = False
    magnet: MagnetInfo | None = None
    job_status: DownloadStatus | None = None

    @property
    def ok(self) -> bool:
        return self.accepted and self.job_status is not DownloadStatus.FAILED


REJECTION_MESSAGES: dict[GateReason, str] = {
    GateReason.BLACKLISTED: "该编号在黑名单中，已拒绝投递。",
    GateReason.ALREADY_ACCEPTED: "该内容此前已接受过，不会重复下载。",
    GateReason.INFO_HASH_IN_JOBS: "相同种子已存在下载任务（活动或已完成），不会重复投递。",
    GateReason.SOURCE_ITEM_ALREADY_PROCESSED: "该磁链已经处理过了。",
    GateReason.INVALID_CANDIDATE: "无法识别这个磁链的内容标识。",
}


class ManualIngestService:
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

    async def ingest(
        self,
        *,
        text: str,
        user_id: int,
        category: str | None = None,
    ) -> IngestResult:
        try:
            magnet = parse_magnet(text)
        except MagnetError as exc:
            return IngestResult(
                accepted=False,
                reason=GateReason.INVALID_CANDIDATE,
                message=f"磁链解析失败：{exc.message}",
            )

        # The FC2 number usually lives in dn=, but users often paste the code
        # next to the magnet as well.
        normalized = normalize_candidate(f"{magnet.display_name or ''} {text}", magnet.info_hash)

        resolved_category = category
        if normalized.content_type is ContentType.FC2:
            resolved_category = "fc2"
        elif resolved_category is None:
            return IngestResult(
                accepted=False,
                reason=GateReason.ALLOWED,
                message="请选择分类：",
                needs_category=True,
                magnet=magnet,
                display_code=normalized.display_code,
            )
        elif resolved_category not in CATEGORIES:
            return IngestResult(
                accepted=False,
                reason=GateReason.INVALID_CANDIDATE,
                message=f"未知分类：{resolved_category}",
            )

        subject = GateSubject(
            content_type=normalized.content_type,
            canonical_key=normalized.canonical_key,
            display_code=normalized.display_code,
            info_hash=magnet.info_hash,
            source="manual",
            source_item_id=magnet.info_hash,
            source_url=None,
            magnet=magnet.raw,
            raw_title=magnet.display_name,
            raw_payload={"user_id": user_id, "display_name": magnet.display_name},
        )
        downloader = self.settings.downloader_for_category(resolved_category)

        decision = None
        try:
            async with self.db.transaction() as session:
                decision = await self.gate.admit(
                    session,
                    subject,
                    origin=AcceptedOrigin.MANUAL,
                    category=resolved_category,
                    downloader=DownloaderName(downloader),
                    actor=f"telegram:{user_id}",
                )
        except GateConflictError:
            # Another task admitted the same content first: report it as
            # "already accepted" rather than failing the user's request.
            _log.info("manual.conflict", canonical_key=normalized.canonical_key)
            decision = None

        if decision is None:
            async with self.db.session() as session:
                decision = await self.gate.evaluate(session, subject)

        if decision.rejected or decision.job_id is None:
            message = REJECTION_MESSAGES.get(
                decision.reason, f"已拒绝投递（{decision.reason.value}）。"
            )
            return IngestResult(
                accepted=False,
                reason=decision.reason,
                message=message,
                category=resolved_category,
                content_id=decision.content_id,
                display_code=normalized.display_code,
                magnet=magnet,
            )

        status = await self.submitter.submit(decision.job_id)
        if status is DownloadStatus.FAILED:
            message = (
                f"已记录接受，但投递到 {downloader} 失败。\n"
                f"可用 /retry {decision.job_id} 重试原任务。"
            )
        else:
            label = normalized.display_code or magnet.info_hash[:12]
            label_text = CATEGORY_LABELS.get(resolved_category, resolved_category)
            message = f"已加入下载队列：{label}\n分类：{label_text}"

        return IngestResult(
            accepted=True,
            reason=GateReason.ALLOWED,
            message=message,
            category=resolved_category,
            job_id=decision.job_id,
            content_id=decision.content_id,
            display_code=normalized.display_code,
            magnet=magnet,
            job_status=status,
        )

    @staticmethod
    def extract_code(text: str) -> str | None:
        """Exposed for the bot's quick reply when a message has no magnet."""

        found = normalize_fc2(text)
        return found.display_code if found else None
