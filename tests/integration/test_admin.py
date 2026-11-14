"""Admin commands — spec 10 (/block, /unblock, /providers)."""

from __future__ import annotations

from app.db.models import BlacklistEntry, DecisionEvent
from app.db.session import Database
from app.services.admin import AdminService
from app.services.metrics import Metrics
from app.services.normalize import Fc2CodeRule
from sqlalchemy import func, select


def service(db: Database, metrics: Metrics | None = None) -> AdminService:
    return AdminService(db=db, metrics=metrics or Metrics(), rule=Fc2CodeRule(5, 8))


async def count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def test_block_normalises_the_code(db: Database) -> None:
    message = await service(db).block("FC2-1234567", actor="telegram:1")

    assert "已加入黑名单" in message
    async with db.session() as session:
        entry = await session.scalar(select(BlacklistEntry))
    assert entry is not None
    assert entry.canonical_key == "fc2_ppv:1234567"
    assert entry.created_by == "telegram:1"


async def test_block_is_idempotent(db: Database) -> None:
    admin = service(db)
    await admin.block("FC2-PPV-1234567", actor="telegram:1")
    second = await admin.block("FC2PPV1234567", actor="telegram:1")

    assert "已在黑名单" in second
    assert await count(db, BlacklistEntry) == 1


async def test_block_rejects_unknown_codes(db: Database) -> None:
    message = await service(db).block("no code here", actor="telegram:1")
    assert "无法识别" in message
    assert await count(db, BlacklistEntry) == 0


async def test_unblock_removes_only_the_blacklist(db: Database) -> None:
    admin = service(db)
    await admin.block("FC2-1234567", actor="telegram:1")
    message = await admin.unblock("FC2-1234567", actor="telegram:1")

    assert "已解除黑名单" in message
    assert await count(db, BlacklistEntry) == 0
    async with db.session() as session:
        events = list(
            (await session.scalars(select(DecisionEvent).order_by(DecisionEvent.created_at))).all()
        )
    assert [event.event_type for event in events] == [
        "content.blacklisted",
        "content.unblocked",
    ]


async def test_unblock_unknown_code_is_reported(db: Database) -> None:
    message = await service(db).unblock("FC2-7654321", actor="telegram:1")
    assert "不在黑名单" in message


async def test_provider_report_shows_hit_miss_structure_and_errors() -> None:
    metrics = Metrics()
    metrics.record_provider("Fc2", "hit", latency_ms=100)
    metrics.record_provider("Fc2", "miss", latency_ms=50)
    metrics.record_provider("Mmtv", "structure", latency_ms=10)
    metrics.record_provider("Mmtv", "structure", latency_ms=10)
    metrics.record_provider("Mmtv", "structure", latency_ms=10)

    report = AdminService(db=None, metrics=metrics, rule=Fc2CodeRule()).providers_report()  # type: ignore[arg-type]

    assert "Fc2: 1 / 1 / 0 / 0" in report
    assert "Mmtv: 0 / 0 / 3 / 0" in report
    assert "需要关注" in report and "Mmtv" in report


async def test_proxy_report_shows_region_and_failure_rate() -> None:
    metrics = Metrics()
    metrics.record_proxy("japan", region="JP", ok=True, latency_ms=100)
    metrics.record_proxy("japan", region="JP", ok=False, latency_ms=900, error="ConnectTimeout")

    report = AdminService(db=None, metrics=metrics, rule=Fc2CodeRule()).proxies_report()  # type: ignore[arg-type]

    assert "japan [JP]" in report
    assert "50%" in report
