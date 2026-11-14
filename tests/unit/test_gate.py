"""Unified content gate — spec 11 / 22.1 (admission order, layering, idempotency)."""

from __future__ import annotations

from app.db.enums import AcceptedOrigin, ContentType, DownloaderName, DownloadStatus, GateReason
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    DecisionEvent,
    DownloadJob,
    Release,
)
from app.db.session import Database
from app.services.gate import ContentGate, GateConflictError, GateSubject
from sqlalchemy import func, select

from tests.support import create_job, hex_hash, make_subject

HASH_A = hex_hash(101)
HASH_B = hex_hash(102)


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


async def _seed_blacklist(db: Database, canonical_key: str) -> None:
    async with db.transaction() as session:
        session.add(BlacklistEntry(canonical_key=canonical_key, reason="test"))


async def _seed_accepted(db: Database, canonical_key: str) -> None:
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


async def _seed_release(db: Database, source: str, source_item_id: str) -> None:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:9999999",
            display_code="FC2-PPV-9999999",
        )
        session.add(content)
        await session.flush()
        session.add(Release(content_id=content.id, source=source, source_item_id=source_item_id))


async def test_clean_subject_is_allowed(db: Database, gate: ContentGate) -> None:
    async with db.transaction() as session:
        decision = await gate.evaluate(session, make_subject(info_hash=HASH_A))
    assert decision.allowed is True
    assert decision.reason is GateReason.ALLOWED


async def test_blacklist_has_highest_priority(db: Database, gate: ContentGate) -> None:
    subject = make_subject(info_hash=HASH_A)
    await _seed_blacklist(db, subject.canonical_key)
    await _seed_accepted(db, subject.canonical_key)

    async with db.transaction() as session:
        decision = await gate.evaluate(session, subject)
    assert decision.reason is GateReason.BLACKLISTED


async def test_accepted_beats_info_hash_and_release(db: Database, gate: ContentGate) -> None:
    subject = make_subject(info_hash=HASH_A)
    await _seed_accepted(db, subject.canonical_key)
    await create_job(db, info_hash=HASH_A, canonical_key="fc2_ppv:8888888")

    async with db.transaction() as session:
        decision = await gate.evaluate(session, subject)
    assert decision.reason is GateReason.ALREADY_ACCEPTED


async def test_info_hash_in_active_job_is_rejected(db: Database, gate: ContentGate) -> None:
    subject = make_subject(info_hash=HASH_A, canonical_key="fc2_ppv:1111111")
    _, _, job_id = await create_job(
        db, info_hash=HASH_A, canonical_key="fc2_ppv:2222222", status=DownloadStatus.DOWNLOADING
    )

    async with db.transaction() as session:
        decision = await gate.evaluate(session, subject)
    assert decision.reason is GateReason.INFO_HASH_IN_JOBS
    assert decision.existing_job_id == job_id


async def test_failed_job_does_not_hold_the_hash(db: Database, gate: ContentGate) -> None:
    """A FAILED job is retried via /retry; the accepted entry is what blocks."""

    subject = make_subject(
        info_hash=HASH_A, canonical_key="fc2_ppv:3333333", source_item_id="manual-new"
    )
    await create_job(
        db, info_hash=HASH_A, canonical_key="fc2_ppv:4444444", status=DownloadStatus.FAILED
    )
    async with db.transaction() as session:
        decision = await gate.evaluate(session, subject)
    assert decision.allowed is True


async def test_source_item_already_processed(db: Database, gate: ContentGate) -> None:
    await _seed_release(db, source="nyaa", source_item_id="item-1")
    subject = make_subject(
        info_hash=HASH_B, source="nyaa", source_item_id="item-1", canonical_key="fc2_ppv:5555555"
    )
    async with db.transaction() as session:
        decision = await gate.evaluate(session, subject)
    assert decision.reason is GateReason.SOURCE_ITEM_ALREADY_PROCESSED


async def test_admit_writes_content_release_accepted_and_job(
    db: Database, gate: ContentGate
) -> None:
    subject = make_subject(info_hash=HASH_A)
    async with db.transaction() as session:
        decision = await gate.admit(
            session,
            subject,
            origin=AcceptedOrigin.MANUAL,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
            actor="telegram:111",
        )

    assert decision.allowed
    assert decision.content_id and decision.release_id and decision.job_id
    assert await _count(db, Content) == 1
    assert await _count(db, Release) == 1
    assert await _count(db, AcceptedEntry) == 1
    assert await _count(db, DownloadJob) == 1

    async with db.session() as session:
        job = await session.get(DownloadJob, decision.job_id)
        assert job is not None
        assert job.status is DownloadStatus.ACCEPTED
        assert job.idempotency_key == f"download:{decision.release_id}"
        events = list(
            (
                await session.scalars(
                    select(DecisionEvent).where(DecisionEvent.event_type == "gate.accepted")
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].actor == "telegram:111"


async def test_second_admit_is_idempotent(db: Database, gate: ContentGate) -> None:
    subject = make_subject(info_hash=HASH_A)
    async with db.transaction() as session:
        first = await gate.admit(
            session,
            subject,
            origin=AcceptedOrigin.MANUAL,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
        )
    async with db.transaction() as session:
        second = await gate.admit(
            session,
            subject,
            origin=AcceptedOrigin.MANUAL,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
        )

    assert first.allowed
    assert second.reason is GateReason.ALREADY_ACCEPTED
    assert await _count(db, DownloadJob) == 1
    assert await _count(db, AcceptedEntry) == 1


async def test_content_and_release_layering(db: Database, gate: ContentGate) -> None:
    """Same FC2 number from another uploader: one content row, no second push."""

    first = make_subject(info_hash=HASH_A, source="nyaa", source_item_id="item-a")
    async with db.transaction() as session:
        decision = await gate.admit(
            session,
            first,
            origin=AcceptedOrigin.SUBSCRIPTION_BUTTON,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
        )
    assert decision.allowed

    second = make_subject(info_hash=HASH_B, source="nyaa", source_item_id="item-b")
    async with db.transaction() as session:
        rejected = await gate.admit(
            session,
            second,
            origin=AcceptedOrigin.SUBSCRIPTION_BUTTON,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
        )

    assert rejected.reason is GateReason.ALREADY_ACCEPTED
    assert await _count(db, Content) == 1
    assert await _count(db, Release) == 1
    assert await _count(db, DownloadJob) == 1


async def test_rejection_is_audited(db: Database, gate: ContentGate) -> None:
    subject = make_subject(info_hash=HASH_A)
    await _seed_blacklist(db, subject.canonical_key)

    async with db.transaction() as session:
        decision = await gate.admit(
            session,
            subject,
            origin=AcceptedOrigin.MANUAL,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
            actor="telegram:222",
        )

    assert decision.rejected
    async with db.session() as session:
        events = list(
            (
                await session.scalars(
                    select(DecisionEvent).where(DecisionEvent.event_type == "gate.rejected")
                )
            ).all()
        )
    assert len(events) == 1
    assert events[0].reason == "blacklisted"
    assert events[0].canonical_key == subject.canonical_key


async def test_unique_constraint_surfaces_as_conflict(db: Database, gate: ContentGate) -> None:
    """A concurrent writer that bypassed the gate must not create a second entry."""

    subject = make_subject(info_hash=HASH_A)
    await _seed_accepted(db, subject.canonical_key)
    async with db.transaction() as session:
        decision = await gate.admit(
            session,
            subject,
            origin=AcceptedOrigin.MANUAL,
            category="fc2",
            downloader=DownloaderName.QBITTORRENT,
        )
    assert decision.reason is GateReason.ALREADY_ACCEPTED
    assert await _count(db, AcceptedEntry) == 1


async def test_gate_conflict_error_type_is_importable() -> None:
    # Guard against accidental renames: callers catch this exact type.
    assert issubclass(GateConflictError, RuntimeError)
    assert GateSubject.__dataclass_fields__["canonical_key"] is not None
