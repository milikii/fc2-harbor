"""Download state machine — spec 13 / 22.1 (conditional updates, timestamps, errors)."""

from __future__ import annotations

import pytest
from app.db.enums import DownloadStatus
from app.db.models import DecisionEvent, DownloadJob
from app.db.session import Database
from app.services.state import (
    StaleTransitionError,
    TransitionError,
    UnknownJobError,
    transition_job,
)
from sqlalchemy import select

from tests.support import create_job, hex_hash

S = DownloadStatus


async def _job(db: Database, job_id: str) -> DownloadJob:
    async with db.session() as session:
        job = await session.get(DownloadJob, job_id)
        assert job is not None
        return job


async def test_accepted_to_submitted_records_timestamp_and_event(db: Database) -> None:
    _, _, job_id = await create_job(db, info_hash=hex_hash(1))

    async with db.transaction() as session:
        result = await transition_job(session, job_id, S.SUBMITTED)

    assert result.from_status is S.ACCEPTED
    job = await _job(db, job_id)
    assert job.status is S.SUBMITTED
    assert job.submitted_at is not None

    async with db.session() as session:
        events = list(
            (
                await session.scalars(select(DecisionEvent).where(DecisionEvent.job_id == job_id))
            ).all()
        )
    assert [event.event_type for event in events] == ["job.status"]
    assert events[0].from_status == "ACCEPTED"
    assert events[0].to_status == "SUBMITTED"


async def test_illegal_transition_is_refused(db: Database) -> None:
    _, _, job_id = await create_job(db, info_hash=hex_hash(2))
    async with db.transaction() as session:
        with pytest.raises(TransitionError):
            await transition_job(session, job_id, S.ORGANIZED)


async def test_stale_expected_status_is_detected(db: Database) -> None:
    _, _, job_id = await create_job(db, info_hash=hex_hash(3))
    async with db.transaction() as session:
        await transition_job(session, job_id, S.SUBMITTED, expected_status=S.ACCEPTED)

    # A second worker still believes the job is ACCEPTED.
    async with db.transaction() as session:
        with pytest.raises(StaleTransitionError):
            await transition_job(session, job_id, S.SUBMITTED, expected_status=S.ACCEPTED)

    job = await _job(db, job_id)
    assert job.status is S.SUBMITTED


async def test_failure_records_stage_error_and_retry_clears_it(db: Database) -> None:
    _, _, job_id = await create_job(db, info_hash=hex_hash(4))

    async with db.transaction() as session:
        await transition_job(
            session, job_id, S.FAILED, error_code="downloader_add_failed", error_message="boom"
        )
    job = await _job(db, job_id)
    assert job.status is S.FAILED
    assert job.error_code == "downloader_add_failed"
    assert job.error_message == "boom"

    async with db.transaction() as session:
        await transition_job(session, job_id, S.ACCEPTED, increment_retry=True)
    job = await _job(db, job_id)
    assert job.status is S.ACCEPTED
    assert job.error_code is None
    assert job.retry_count == 1

    # The failed stage survives in the audit trail.
    async with db.session() as session:
        events = list(
            (
                await session.scalars(select(DecisionEvent).where(DecisionEvent.job_id == job_id))
            ).all()
        )
    assert events[0].error_code == "downloader_add_failed"


async def test_unknown_job_is_reported(db: Database) -> None:
    async with db.transaction() as session:
        with pytest.raises(UnknownJobError):
            await transition_job(session, "does-not-exist", S.SUBMITTED)


async def test_organized_requires_downloaded_first(db: Database) -> None:
    _, _, job_id = await create_job(db, info_hash=hex_hash(5), status=S.DOWNLOADED)
    async with db.transaction() as session:
        with pytest.raises(TransitionError):
            await transition_job(session, job_id, S.TASK_REMOVED)
