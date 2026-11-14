"""Admin actions behind the bot commands — spec 10.

Blacklist management and the provider report live here so they can be tested
without aiogram. ``/unblock`` removes the blacklist entry but deliberately keeps the
accepted record: re-accepting a content is an explicit decision, not a side effect
(spec 10, spec 11.3).
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, select

from app.db.models import BlacklistEntry
from app.db.session import Database
from app.logging import get_logger
from app.services.metrics import Metrics
from app.services.normalize import Fc2CodeRule
from app.services.state import record_event

_log = get_logger("admin")


class AdminService:
    def __init__(self, *, db: Database, metrics: Metrics, rule: Fc2CodeRule) -> None:
        self.db = db
        self.metrics = metrics
        self.rule = rule

    async def block(self, code_text: str, *, actor: str) -> str:
        normalized = self.rule.normalize(code_text)
        if normalized is None:
            return "无法识别 FC2 编号，用法：/block FC2-PPV-1234567"
        async with self.db.transaction() as session:
            existing = await session.scalar(
                select(BlacklistEntry.id).where(
                    BlacklistEntry.canonical_key == normalized.canonical_key
                )
            )
            if existing is None:
                session.add(
                    BlacklistEntry(
                        canonical_key=normalized.canonical_key,
                        reason="admin_block",
                        created_by=actor,
                    )
                )
            await record_event(
                session,
                "content.blacklisted",
                canonical_key=normalized.canonical_key,
                actor=actor,
                reason="admin_block" if existing is None else "already_blocked",
            )
        _log.info("admin.block", canonical_key=normalized.canonical_key, actor=actor)
        if existing is not None:
            return f"{normalized.display_code} 已在黑名单中。"
        return f"已加入黑名单：{normalized.display_code}"

    async def unblock(self, code_text: str, *, actor: str) -> str:
        normalized = self.rule.normalize(code_text)
        if normalized is None:
            return "无法识别 FC2 编号，用法：/unblock FC2-PPV-1234567"
        async with self.db.transaction() as session:
            result = cast(
                "CursorResult[Any]",
                await session.execute(
                    delete(BlacklistEntry).where(
                        BlacklistEntry.canonical_key == normalized.canonical_key
                    )
                ),
            )
            removed = bool(result.rowcount)
            await record_event(
                session,
                "content.unblocked",
                canonical_key=normalized.canonical_key,
                actor=actor,
                reason="admin_unblock" if removed else "not_blocked",
            )
        _log.info(
            "admin.unblock", canonical_key=normalized.canonical_key, removed=removed, actor=actor
        )
        if not removed:
            return f"{normalized.display_code} 不在黑名单中。"
        return f"已解除黑名单：{normalized.display_code}\n已接受记录保留，不会因此重新推送。"

    def providers_report(self) -> str:
        snapshot = self.metrics.provider_snapshot()
        if not snapshot:
            return "还没有 provider 调用记录。"
        lines = ["provider 健康（hit / miss / 结构变化 / 错误 / 平均耗时）"]
        for name, data in snapshot.items():
            lines.append(
                f"{name}: {data['hit']} / {data['miss']} / {data['structure']} / "
                f"{data['error']} / {data['average_latency_ms']}ms "
                f"(命中率 {data['hit_rate']:.0%})"
            )
        unhealthy = self.metrics.unhealthy_providers()
        if unhealthy:
            lines.append("")
            lines.append("需要关注：" + "、".join(unhealthy))
        return "\n".join(lines)

    def proxies_report(self) -> str:
        snapshot = self.metrics.proxy_snapshot()
        if not snapshot:
            return "还没有代理调用记录。"
        lines = ["代理出口（region / 调用 / 失败率 / 平均耗时）"]
        for name, data in snapshot.items():
            lines.append(
                f"{name} [{data['region'] or '-'}]: {data['calls']} / "
                f"{data['failure_rate']:.0%} / {data['average_latency_ms']}ms"
            )
        return "\n".join(lines)
