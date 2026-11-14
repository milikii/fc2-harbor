"""Release registration — spec 6.4 / 11 (gate before release, candidate sources).

This is where the M2 rule lives: an already-accepted canonical key still gets its
new source item stored as a *candidate download source*, but no job, no accepted
entry and no push.
"""

from __future__ import annotations

from app.db.enums import AcceptedOrigin, ContentType, GateReason
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    DecisionEvent,
    DownloadJob,
    Release,
)
from app.db.session import Database
from app.schemas.release import NormalizedContent, ReleaseCandidate
from app.services.gate import ContentGate
from app.services.normalize import canonical_fc2_key, display_fc2_code
from app.services.releases import RegistrationOutcome, ReleaseRegistrar
from sqlalchemy import func, select

from tests.support import hex_hash

HASH_A = hex_hash(501)
HASH_B = hex_hash(502)
HASH_C = hex_hash(503)


def make_candidate(
    *,
    item_id: str = "1900001",
    number: str = "1234567",
    info_hash: str | None = HASH_A,
    source: str = "nyaa",
    title: str | None = None,
) -> ReleaseCandidate:
    return ReleaseCandidate(
        source=source,
        source_item_id=item_id,
        source_url=f"https://sukebei.nyaa.si/view/{item_id}",
        title=title or f"FC2-PPV-{number} title",
        published_at=None,
        magnet=f"magnet:?xt=urn:btih:{info_hash}" if info_hash else None,
        info_hash=info_hash,
        content_hint=NormalizedContent(
            content_type=ContentType.FC2,
            canonical_key=canonical_fc2_key(number),
            display_code=display_fc2_code(number),
        ),
        raw_payload={"uploader": "Uploader A"},
    )


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _seed_accepted(db: Database, canonical_key: str) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=canonical_key,
            display_code="FC2-PPV-1234567",
        )
        session.add(content)
        await session.flush()
        session.add(
            AcceptedEntry(
                canonical_key=canonical_key,
                content_id=content.id,
                origin=AcceptedOrigin.MANUAL,
            )
        )
        return content.id


async def test_allowed_candidate_records_content_and_release_only(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    result = await registrar.handle(make_candidate())

    assert result.outcome is RegistrationOutcome.RECORDED
    assert await _count(db, Content) == 1
    assert await _count(db, Release) == 1
    # Discovery never accepts or downloads anything by itself.
    assert await _count(db, AcceptedEntry) == 0
    assert await _count(db, DownloadJob) == 0

    async with db.session() as session:
        release = await session.scalar(select(Release))
        assert release is not None
        assert release.magnet is not None
        assert release.raw_title is not None
        event = await session.scalar(
            select(DecisionEvent).where(DecisionEvent.event_type == "release.discovered")
        )
    assert event is not None and event.release_id == release.id


async def test_same_content_from_another_uploader_adds_a_second_release(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    first = await registrar.handle(make_candidate(item_id="1900001", info_hash=HASH_A))
    second = await registrar.handle(make_candidate(item_id="1900002", info_hash=HASH_B))

    assert first.outcome is RegistrationOutcome.RECORDED
    assert second.outcome is RegistrationOutcome.RECORDED
    assert await _count(db, Content) == 1
    assert await _count(db, Release) == 2
    assert await _count(db, DownloadJob) == 0


async def test_accepted_content_still_records_the_candidate_release(db: Database) -> None:
    canonical = canonical_fc2_key("1234567")
    await _seed_accepted(db, canonical)
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    result = await registrar.handle(make_candidate(item_id="1900009", info_hash=HASH_C))

    assert result.outcome is RegistrationOutcome.CANDIDATE
    assert result.reason is GateReason.ALREADY_ACCEPTED
    assert await _count(db, Release) == 1
    assert await _count(db, AcceptedEntry) == 1  # unchanged
    assert await _count(db, DownloadJob) == 0  # no second job

    async with db.session() as session:
        event = await session.scalar(
            select(DecisionEvent).where(DecisionEvent.event_type == "release.candidate_recorded")
        )
    assert event is not None and event.reason == "already_accepted"


async def test_repeated_candidate_is_a_duplicate(db: Database) -> None:
    canonical = canonical_fc2_key("1234567")
    await _seed_accepted(db, canonical)
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    candidate = make_candidate(item_id="1900009", info_hash=HASH_C)

    assert (await registrar.handle(candidate)).outcome is RegistrationOutcome.CANDIDATE
    assert (await registrar.handle(candidate)).outcome is RegistrationOutcome.DUPLICATE
    assert await _count(db, Release) == 1


async def test_blacklisted_content_is_not_stored(db: Database) -> None:
    canonical = canonical_fc2_key("1234567")
    async with db.transaction() as session:
        session.add(BlacklistEntry(canonical_key=canonical, reason="user said no"))
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    result = await registrar.handle(make_candidate())

    assert result.outcome is RegistrationOutcome.BLACKLISTED
    # The gate ran before any write: no content, no release, only the audit row.
    assert await _count(db, Content) == 0
    assert await _count(db, Release) == 0
    async with db.session() as session:
        event = await session.scalar(
            select(DecisionEvent).where(DecisionEvent.event_type == "gate.rejected")
        )
    assert event is not None and event.reason == "blacklisted"


async def test_duplicate_source_item_is_skipped(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    candidate = make_candidate()

    assert (await registrar.handle(candidate)).outcome is RegistrationOutcome.RECORDED
    assert (await registrar.handle(candidate)).outcome is RegistrationOutcome.DUPLICATE
    assert await _count(db, Release) == 1


async def test_info_hash_collision_is_reported_as_duplicate(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    # A release for another content already owns HASH_A.
    assert (
        await registrar.handle(
            make_candidate(item_id="1900001", number="1111111", info_hash=HASH_A)
        )
    ).outcome is RegistrationOutcome.RECORDED

    collision = await registrar.handle(
        make_candidate(item_id="1900002", number="2222222", info_hash=HASH_A)
    )

    assert collision.outcome is RegistrationOutcome.DUPLICATE
    assert await _count(db, Release) == 1


async def test_candidate_without_content_hint_is_ignored(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    candidate = ReleaseCandidate(
        source="nyaa",
        source_item_id="1900100",
        source_url=None,
        title="no code here",
        info_hash=HASH_A,
        content_hint=None,
    )

    result = await registrar.handle(candidate)

    assert result.outcome is RegistrationOutcome.DUPLICATE
    assert result.reason is GateReason.INVALID_CANDIDATE
    assert await _count(db, Content) == 0
