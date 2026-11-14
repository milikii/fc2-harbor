"""Read-only "what still needs a card" query — spec 6.4 / 9.

M2 records discoveries; M4 turns them into Telegram cards. The driver query lives
here now so M4 does not have to touch the gate:

a content qualifies when it has at least one release, is **not** accepted, is
**not** blacklisted, and has **no** notification row yet.

Only the earliest release of each content is returned (spec 6.4: the first
release triggers the push, later uploaders are candidate sources), together with
a release count so the card can mention alternatives. Nothing here writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AcceptedEntry, BlacklistEntry, Content, Notification, Release

DEFAULT_PUSH_BATCH = 50


@dataclass(frozen=True, slots=True)
class PushCandidate:
    content_id: str
    canonical_key: str
    display_code: str | None
    title_ja: str | None
    release_id: str
    source: str
    source_item_id: str
    source_url: str | None
    magnet: str | None
    info_hash: str | None
    published_at: datetime | None
    release_count: int


async def find_pending_push_candidates(
    session: AsyncSession, *, limit: int = DEFAULT_PUSH_BATCH
) -> list[PushCandidate]:
    """Contents discovered but never accepted, blacklisted or notified."""

    if limit <= 0:
        return []

    ranked = select(
        Release.id.label("release_id"),
        func.row_number()
        .over(
            partition_by=Release.content_id,
            # The original publication wins over insertion order, so a card
            # points at the first uploader even when pagination inserted a
            # later copy first. Unknown publication dates fall back to the
            # insertion order.
            order_by=(
                Release.published_at.asc().nullslast(),
                Release.created_at,
                Release.id,
            ),
        )
        .label("rn"),
    ).subquery()
    release_count = (
        select(func.count(Release.id))
        .where(Release.content_id == Content.id)
        .correlate(Content)
        .scalar_subquery()
    )

    stmt = (
        select(Content, Release, release_count.label("release_count"))
        .join(Release, Release.content_id == Content.id)
        .join(ranked, and_(ranked.c.release_id == Release.id, ranked.c.rn == 1))
        .where(
            Content.canonical_key.not_in(select(AcceptedEntry.canonical_key)),
            Content.canonical_key.not_in(select(BlacklistEntry.canonical_key)),
            Content.id.not_in(select(Notification.content_id)),
        )
        .order_by(Release.published_at.desc().nullslast(), Content.created_at.asc())
        .limit(limit)
    )

    rows = (await session.execute(stmt)).all()
    return [
        PushCandidate(
            content_id=content.id,
            canonical_key=content.canonical_key,
            display_code=content.display_code,
            title_ja=content.title_ja,
            release_id=release.id,
            source=release.source,
            source_item_id=release.source_item_id,
            source_url=release.source_url,
            magnet=release.magnet,
            info_hash=release.info_hash,
            published_at=release.published_at,
            release_count=int(count),
        )
        for content, release, count in rows
    ]
