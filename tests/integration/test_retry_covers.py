"""``scripts/retry_covers.py`` — give give_up rows another chance."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.db.enums import AssetRole, ContentType, CoverStatus
from app.db.models import Asset, Content
from app.db.session import Database
from scripts.retry_covers import RetryReport, _DryRunRollbackError, retry_covers
from sqlalchemy import select


async def seed(
    db: Database,
    key: str,
    *,
    status: CoverStatus = CoverStatus.GIVE_UP,
    retries: int = 3,
    created_at: datetime | None = None,
) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=key,
            display_code="FC2-PPV-1234567",
            cover_status=status.value,
            cover_retry_count=retries,
            created_at=created_at or datetime.now(UTC),
        )
        session.add(content)
        await session.flush()
        return content.id


async def state(db: Database, content_id: str) -> tuple[str, int]:
    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        return content.cover_status, content.cover_retry_count


async def test_give_up_rows_are_reset(db: Database) -> None:
    content_id = await seed(db, "fc2_ppv:1")
    report = RetryReport(dry_run=False)

    async with db.transaction() as session:
        await retry_covers(session, report=report)

    assert report.reset == 1
    assert await state(db, content_id) == (CoverStatus.PENDING.value, 0)


async def test_other_statuses_are_untouched(db: Database) -> None:
    ready = await seed(db, "fc2_ppv:1", status=CoverStatus.READY, retries=0)
    pending = await seed(db, "fc2_ppv:2", status=CoverStatus.PENDING, retries=0)
    waiting = await seed(db, "fc2_ppv:3", status=CoverStatus.PENDING_COVER, retries=1)
    report = RetryReport(dry_run=False)

    async with db.transaction() as session:
        await retry_covers(session, report=report)

    assert report.reset == 0
    assert await state(db, ready) == (CoverStatus.READY.value, 0)
    assert await state(db, pending) == (CoverStatus.PENDING.value, 0)
    assert await state(db, waiting) == (CoverStatus.PENDING_COVER.value, 1)


async def test_dry_run_writes_nothing(db: Database) -> None:
    content_id = await seed(db, "fc2_ppv:1")
    report = RetryReport(dry_run=True)

    try:
        async with db.transaction() as session:
            await retry_covers(session, report=report)
            raise _DryRunRollbackError
    except _DryRunRollbackError:
        pass

    assert report.reset == 1
    assert await state(db, content_id) == (CoverStatus.GIVE_UP.value, 3)


async def test_since_filters_by_creation_date(db: Database) -> None:
    fresh = await seed(db, "fc2_ppv:1")
    old = await seed(db, "fc2_ppv:2", created_at=datetime.now(UTC) - timedelta(days=30))
    report = RetryReport(dry_run=False)

    async with db.transaction() as session:
        await retry_covers(session, since_days=7, report=report)

    assert report.reset == 1
    assert await state(db, fresh) == (CoverStatus.PENDING.value, 0)
    assert await state(db, old) == (CoverStatus.GIVE_UP.value, 3)


async def test_only_filters_by_canonical_key(db: Database) -> None:
    first = await seed(db, "fc2_ppv:1")
    second = await seed(db, "fc2_ppv:2")
    report = RetryReport(dry_run=False)

    async with db.transaction() as session:
        await retry_covers(session, only="fc2_ppv:2", report=report)

    assert report.reset == 1
    assert await state(db, first) == (CoverStatus.GIVE_UP.value, 3)
    assert await state(db, second) == (CoverStatus.PENDING.value, 0)


async def test_second_run_is_idempotent(db: Database) -> None:
    await seed(db, "fc2_ppv:1")
    for _ in range(2):
        report = RetryReport(dry_run=False)
        async with db.transaction() as session:
            await retry_covers(session, report=report)
        if _ == 0:
            assert report.reset == 1
        else:
            assert report.reset == 0


async def test_updated_at_is_not_touched(db: Database) -> None:
    stamp = datetime(2020, 11, 1, 12, 0)
    content_id = await seed(db, "fc2_ppv:1")
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.updated_at = stamp

    report = RetryReport(dry_run=False)
    async with db.transaction() as session:
        await retry_covers(session, report=report)

    async with db.session() as session:
        content = await session.scalar(select(Content).where(Content.id == content_id))
    assert content is not None
    assert content.updated_at == stamp
    assert report.reset == 1


# -- --min-cover-dimension: drop 300x250 ad banners ----------------------------


async def seed_with_cover(
    db: Database, key: str, *, width: int, height: int, status: CoverStatus = CoverStatus.READY
) -> str:
    content_id = await seed(db, key, status=status, retries=0)
    async with db.transaction() as session:
        session.add(
            Asset(
                content_id=content_id,
                provider="Nyaa",
                role=AssetRole.COVER,
                original_url=f"https://cdn.example/{key}.jpg",
                normalized_url=f"https://cdn.example/{key}.jpg",
                sha256="a" * 64,
                local_path="/tmp/x.jpg",
                width=width,
                height=height,
                size_bytes=1024,
                position=0,
            )
        )
    return content_id


async def test_small_cover_is_reset_only_with_the_flag(db: Database) -> None:
    small = await seed_with_cover(db, "fc2_ppv:1", width=300, height=250)
    big = await seed_with_cover(db, "fc2_ppv:2", width=800, height=600)

    report = RetryReport(dry_run=False)
    async with db.transaction() as session:
        await retry_covers(session, report=report)
    assert report.reset == 0  # nothing to do without the flag
    assert await state(db, small) == (CoverStatus.READY.value, 0)

    report = RetryReport(dry_run=False)
    async with db.transaction() as session:
        await retry_covers(session, min_cover_dimension=400, report=report)

    assert report.reset == 1
    assert report.rows[0]["canonical_key"] == "fc2_ppv:1"
    assert report.rows[0]["reason"] == "small_cover"
    assert await state(db, small) == (CoverStatus.PENDING.value, 0)
    assert await state(db, big) == (CoverStatus.READY.value, 0)


async def test_give_up_and_small_cover_are_counted_once(db: Database) -> None:
    content_id = await seed_with_cover(
        db, "fc2_ppv:1", width=300, height=250, status=CoverStatus.GIVE_UP
    )
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.cover_retry_count = 3

    report = RetryReport(dry_run=False)
    async with db.transaction() as session:
        await retry_covers(session, min_cover_dimension=400, report=report)

    assert report.reset == 1
    assert await state(db, content_id) == (CoverStatus.PENDING.value, 0)
