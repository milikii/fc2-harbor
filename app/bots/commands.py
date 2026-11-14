"""Admin commands for the manual bot — spec 10.

Only whitelisted users in a private chat may use them; every handler re-checks
the identity instead of relying on hidden buttons (spec 20).

``/retry`` retries the *original* job at the stage it failed at and never creates
a new content/job (spec 11.3). ``/block``, ``/unblock``, ``/pause``,
``/resume`` and ``/providers`` are M5.
"""

from __future__ import annotations

from collections.abc import Iterable

from aiogram import F, Router
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select

from app.db.enums import DownloadStatus
from app.db.models import DownloadJob
from app.db.session import Database
from app.logging import get_logger
from app.services.jobs import JobNotFoundError, JobSubmitter

_log = get_logger("bot.manual.commands")

HELP_TEXT = """可用命令：
/status — 运行状态与最近失败
/retry <job_id> — 重试原下载任务
直接发送 magnet 链接即可投递（FC2 编号自动识别，其余需选分类）。"""


class WhitelistFilter(BaseFilter):
    """Allow only whitelisted users, only in private chats."""

    def __init__(self, allowed_user_ids: Iterable[int]) -> None:
        self.allowed = frozenset(allowed_user_ids)

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        if user is None or user.id not in self.allowed:
            _log.warning("bot.unauthorized", user_id=user.id if user else None)
            if isinstance(event, CallbackQuery):
                await event.answer("无权限", show_alert=False)
            return False

        chat_type = _chat_type(event)
        if chat_type != "private":
            _log.warning("bot.rejected_non_private", user_id=user.id, chat_type=chat_type)
            return False
        return True


def _chat_type(event: Message | CallbackQuery) -> str:
    if isinstance(event, CallbackQuery):
        return event.message.chat.type if event.message else "unknown"
    return event.chat.type


def build_commands_router(
    *,
    db: Database,
    submitter: JobSubmitter,
    allowed_user_ids: Iterable[int],
) -> Router:
    router = Router(name="manual-commands")
    router.message.filter(WhitelistFilter(allowed_user_ids))
    router.callback_query.filter(WhitelistFilter(allowed_user_ids))

    @router.message(CommandStart())
    async def handle_start(message: Message) -> None:
        await message.answer("手动投递 Bot 已就绪。\n\n" + HELP_TEXT)

    @router.message(Command("help"))
    async def handle_help(message: Message) -> None:
        await message.answer(HELP_TEXT)

    @router.message(Command("status"))
    async def handle_status(message: Message) -> None:
        await message.answer(await _status_text(db))

    @router.message(Command("retry"))
    async def handle_retry(message: Message) -> None:
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2 or not parts[1].strip():
            await message.answer("用法：/retry <job_id>")
            return
        job_id = parts[1].strip()
        try:
            status = await submitter.retry(job_id)
        except JobNotFoundError:
            await message.answer(f"找不到任务 {job_id}")
            return
        await message.answer(f"任务 {job_id} 已重试，当前状态：{status.value}")

    @router.message(F.text.startswith("/"))
    async def handle_unknown(message: Message) -> None:
        await message.answer("未知命令。\n\n" + HELP_TEXT)

    return router


async def _status_text(db: Database) -> str:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(DownloadJob.status, func.count()).group_by(DownloadJob.status)
            )
        ).all()
        failed = list(
            (
                await session.scalars(
                    select(DownloadJob)
                    .where(DownloadJob.status == DownloadStatus.FAILED)
                    .order_by(DownloadJob.created_at.desc())
                    .limit(5)
                )
            ).all()
        )

    if not rows:
        return "运行状态：暂无任务。"

    lines = ["运行状态"]
    for status_value, count in sorted(rows, key=lambda row: row[0].value):
        lines.append(f"{status_value.value}: {count}")
    if failed:
        lines.append("")
        lines.append("最近失败：")
        for job in failed:
            lines.append(f"{job.id} — {job.error_code or 'unknown'}")
    return "\n".join(lines)
