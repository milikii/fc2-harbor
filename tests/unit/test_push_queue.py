"""Pending push query — spec 6.4 / 9 (read-only, used by M4)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from app.db.enums import AcceptedOrigin, ContentType, NotificationStatus
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    Notification,
    Release,
)
from app.db.session import Database
from app.services.push_queue import find_pending_push_candidates


async def seed_content(
    db: Database,
    *,
    number: str = "1234567",
    releases: list[tuple[str, str, datetime | None]] | None = None,
    accepted: bool = False,
    blacklisted: bool = False,
    notified: bool = False,
) -> str:
    """Create a content with releases and optional accepted/blacklist/notification."""

    canonical = f"fc2_ppv:{number}"
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=canonical,
            display_code=f"FC2-PPV-{number}",
            title_ja=f"FC2-PPV-{number} title",
        )
        session.add(content)
        await session.flush()

        for source, item_id, published in releases or [
            ("nyaa", f"item-{number}", datetime.now(UTC))
        ]:
            # Unique per (content, item): the info-hash index is global.
            info_hash = hashlib.blake2b(f"{number}:{item_id}".encode(), digest_size=20).hexdigest()
            session.add(
                Release(
                    content_id=content.id,
                    source=source,
                    source_item_id=item_id,
                    source_url=f"https://sukebei.nyaa.si/view/{item_id}",
                    magnet=f"magnet:?xt=urn:btih:{info_hash}",
                    info_hash=info_hash,
                    published_at=published,
                )
            )
        if accepted:
            session.add(
                AcceptedEntry(
                    canonical_key=canonical,
                    content_id=content.id,
                    origin=AcceptedOrigin.MANUAL,
                )
            )
        if blacklisted:
            session.add(BlacklistEntry(canonical_key=canonical, reason="test"))
        if notified:
            session.add(
                Notification(
                    content_id=content.id,
                    bot_name="subscription",
                    chat_id=111,
                    status=NotificationStatus.SENT,
                )
            )
        return content.id


async def test_discovered_content_is_pending(db: Database) -> None:
    await seed_content(db)

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.canonical_key == "fc2_ppv:1234567"
    assert candidate.display_code == "FC2-PPV-1234567"
    assert candidate.source == "nyaa"
    assert candidate.magnet is not None
    assert candidate.release_count == 1


async def test_accepted_blacklisted_and_notified_are_excluded(db: Database) -> None:
    await seed_content(db, number="1111111", accepted=True)
    await seed_content(db, number="2222222", blacklisted=True)
    await seed_content(db, number="3333333", notified=True)
    await seed_content(db, number="4444444")

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert [c.canonical_key for c in candidates] == ["fc2_ppv:4444444"]


async def test_only_the_earliest_release_is_offered(db: Database) -> None:
    now = datetime.now(UTC)
    await seed_content(
        db,
        number="5555555",
        releases=[
            ("nyaa", "item-late", now),
            ("nyaa", "item-early", now - timedelta(hours=2)),
        ],
    )

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert len(candidates) == 1
    assert candidates[0].source_item_id == "item-early"
    assert candidates[0].release_count == 2


async def test_newest_published_first(db: Database) -> None:
    now = datetime.now(UTC)
    await seed_content(
        db,
        number="6666666",
        releases=[("nyaa", "old", now - timedelta(days=1))],
    )
    await seed_content(
        db,
        number="7777777",
        releases=[("nyaa", "new", now)],
    )

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert [c.canonical_key for c in candidates] == ["fc2_ppv:7777777", "fc2_ppv:6666666"]


async def test_pending_notification_keeps_the_content_in_the_queue(db: Database) -> None:
    """A half-sent card must be resumable, so pending does not exclude it."""

    content_id = await seed_content(db, number="8888888")
    async with db.transaction() as session:
        session.add(
            Notification(
                content_id=content_id,
                bot_name="subscription",
                chat_id=111,
                status=NotificationStatus.PENDING,
            )
        )

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert [c.canonical_key for c in candidates] == ["fc2_ppv:8888888"]


async def test_limit_is_respected(db: Database) -> None:
    for index in range(5):
        await seed_content(db, number=f"900000{index}")

    async with db.session() as session:
        assert len(await find_pending_push_candidates(session, limit=2)) == 2
        assert len(await find_pending_push_candidates(session, limit=0)) == 0


# -- cover gate interaction ---------------------------------------------------


async def set_cover_status(db: Database, canonical_key: str, status: str, retries: int = 0) -> None:
    from sqlalchemy import select

    async with db.transaction() as session:
        content = await session.scalar(
            select(Content).where(Content.canonical_key == canonical_key)
        )
        assert content is not None
        content.cover_status = status
        content.cover_retry_count = retries


async def test_give_up_contents_are_excluded_from_the_queue(db: Database) -> None:
    """A row that will never get a cover must not occupy a batch slot forever."""

    from app.db.enums import CoverStatus

    await seed_content(db, number="1000001")
    await seed_content(db, number="1000002")
    await set_cover_status(db, "fc2_ppv:1000002", CoverStatus.GIVE_UP.value, retries=3)

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session, limit=10)

    assert [item.canonical_key for item in candidates] == ["fc2_ppv:1000001"]


async def test_pending_cover_rows_stay_in_the_queue(db: Database) -> None:
    """Rows waiting for a cover must stay: the publisher retries them each round."""

    from app.db.enums import CoverStatus

    await seed_content(db, number="1000003")
    await seed_content(db, number="1000004")
    await set_cover_status(db, "fc2_ppv:1000003", CoverStatus.PENDING_COVER.value, retries=1)
    await set_cover_status(db, "fc2_ppv:1000004", CoverStatus.READY.value, retries=0)

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session, limit=10)

    keys = {item.canonical_key for item in candidates}
    assert keys == {"fc2_ppv:1000003", "fc2_ppv:1000004"}
