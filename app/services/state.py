"""Download state machine with conditional (compare-and-set) updates.

Spec references: section 13 (states, timestamps, conditional updates), section
11.2 (idempotent callbacks), section 24 ("task is only removed after the files
are organized").

Every transition is a single ``UPDATE … WHERE id = ? AND status = ?``. If the
row is not in the expected state, the update affects zero rows and
:class:`StaleTransitionError` is raised — that is how two concurrent tasks
(button double click, monitor tick after a restart) are prevented from advancing
the same job twice. Each successful transition writes a ``decision_events`` row,
so the audit trail keeps the failed stage and error code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final, cast

from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import DownloadStatus
from app.db.models import DecisionEvent, DownloadJob, utcnow
from app.logging import get_logger

_log = get_logger("state")

S = DownloadStatus

#: Spec 13 happy path plus retry targets for FAILED.
ALLOWED_TRANSITIONS: Final[dict[DownloadStatus, frozenset[DownloadStatus]]] = {
    S.DISCOVERED: frozenset({S.NOTIFIED, S.ACCEPTED, S.BLOCKED, S.FAILED, S.CANCELLED}),
    S.NOTIFIED: frozenset({S.ACCEPTED, S.BLOCKED, S.FAILED, S.CANCELLED}),
    S.ACCEPTED: frozenset({S.SUBMITTED, S.FAILED, S.CANCELLED}),
    S.SUBMITTED: frozenset({S.DOWNLOADING, S.DOWNLOADED, S.FAILED, S.CANCELLED}),
    S.DOWNLOADING: frozenset({S.DOWNLOADED, S.FAILED, S.CANCELLED}),
    S.DOWNLOADED: frozenset({S.ORGANIZING, S.FAILED}),
    S.ORGANIZING: frozenset({S.ORGANIZED, S.FAILED}),
    S.ORGANIZED: frozenset({S.TASK_REMOVED, S.FAILED}),
    S.TASK_REMOVED: frozenset(),
    # FAILED keeps the failed stage in decision_events; a retry re-enters the
    # stage it failed at. Nothing is deleted, so every target is safe.
    S.FAILED: frozenset(
        {S.ACCEPTED, S.SUBMITTED, S.DOWNLOADING, S.DOWNLOADED, S.ORGANIZING, S.CANCELLED}
    ),
    S.BLOCKED: frozenset(),
    S.CANCELLED: frozenset(),
}

#: Column that records when a job reached a given state (spec 13).
TIMESTAMP_COLUMN: Final[dict[DownloadStatus, str]] = {
    S.SUBMITTED: "submitted_at",
    S.DOWNLOADED: "completed_at",
    S.ORGANIZED: "organized_at",
    S.TASK_REMOVED: "removed_at",
}

#: States that make an info hash "taken" for the gate (spec 11.1 step 3).
ACTIVE_OR_COMPLETED: Final[frozenset[DownloadStatus]] = frozenset(
    {
        S.ACCEPTED,
        S.SUBMITTED,
        S.DOWNLOADING,
        S.DOWNLOADED,
        S.ORGANIZING,
        S.ORGANIZED,
        S.TASK_REMOVED,
    }
)

#: States the monitor polls every tick.
IN_FLIGHT: Final[frozenset[DownloadStatus]] = frozenset({S.SUBMITTED, S.DOWNLOADING})

#: States whose files should be organized.
ORGANIZE_PENDING: Final[frozenset[DownloadStatus]] = frozenset({S.DOWNLOADED, S.ORGANIZING})

#: States reconciled against the downloader at container start (spec 13).
RECONCILE_STATUSES: Final[frozenset[DownloadStatus]] = frozenset(
    {S.ACCEPTED, S.SUBMITTED, S.DOWNLOADING, S.DOWNLOADED, S.ORGANIZING}
)


class TransitionError(RuntimeError):
    """A transition that the state machine does not allow."""


class StaleTransitionError(TransitionError):
    """Another task advanced the job first; the caller should re-read it."""


class UnknownJobError(TransitionError):
    """The job id does not exist."""


@dataclass(frozen=True, slots=True)
class TransitionResult:
    job_id: str
    from_status: DownloadStatus
    to_status: DownloadStatus


def validate_transition(current: DownloadStatus, target: DownloadStatus) -> None:
    if target not in ALLOWED_TRANSITIONS[current]:
        allowed = sorted(status.value for status in ALLOWED_TRANSITIONS[current])
        raise TransitionError(
            f"illegal transition {current.value} -> {target.value}; allowed: {allowed}"
        )


def allowed_next(current: DownloadStatus) -> frozenset[DownloadStatus]:
    return ALLOWED_TRANSITIONS[current]


async def transition_job(
    session: AsyncSession,
    job_id: str,
    to_status: DownloadStatus,
    *,
    expected_status: DownloadStatus | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    increment_retry: bool = False,
    extra_values: dict[str, object] | None = None,
) -> TransitionResult:
    """Move one job to ``to_status`` atomically.

    ``expected_status`` may be supplied by a caller that already read the row; it
    becomes part of the WHERE clause. When omitted, the current status is read
    first and then used as the condition — still a compare-and-set, never a blind
    write.
    """

    current = expected_status
    if current is None:
        stored = await session.scalar(select(DownloadJob.status).where(DownloadJob.id == job_id))
        if stored is None:
            raise UnknownJobError(f"download job {job_id} not found")
        current = stored

    validate_transition(current, to_status)

    values: dict[str, object] = {
        "status": to_status,
        "error_code": error_code if to_status is S.FAILED else None,
        "error_message": error_message if to_status is S.FAILED else None,
    }
    timestamp_column = TIMESTAMP_COLUMN.get(to_status)
    if timestamp_column is not None:
        values[timestamp_column] = utcnow()
    if increment_retry:
        values["retry_count"] = DownloadJob.retry_count + 1
    if extra_values:
        # Same UPDATE as the status guard, so task ids and paths can never be
        # recorded for a job another task already advanced.
        values.update(extra_values)

    result = cast(
        "CursorResult[Any]",
        await session.execute(
            update(DownloadJob)
            .where(DownloadJob.id == job_id, DownloadJob.status == current)
            .values(**values)
        ),
    )
    if result.rowcount != 1:
        raise StaleTransitionError(
            f"job {job_id} is no longer in {current.value}; another task advanced it"
        )

    session.add(
        DecisionEvent(
            job_id=job_id,
            event_type="job.status",
            from_status=current.value,
            to_status=to_status.value,
            actor=actor,
            reason=reason,
            error_code=error_code,
        )
    )
    _log.info(
        "job.transition",
        job_id=job_id,
        from_status=current.value,
        status=to_status.value,
        error_code=error_code,
        actor=actor,
    )
    return TransitionResult(job_id=job_id, from_status=current, to_status=to_status)


async def record_event(
    session: AsyncSession,
    event_type: str,
    *,
    content_id: str | None = None,
    canonical_key: str | None = None,
    release_id: str | None = None,
    job_id: str | None = None,
    actor: str | None = None,
    reason: str | None = None,
    error_code: str | None = None,
) -> None:
    """Append an audit event that is not a status change (gate, blacklist, …)."""

    session.add(
        DecisionEvent(
            event_type=event_type,
            content_id=content_id,
            canonical_key=canonical_key,
            release_id=release_id,
            job_id=job_id,
            actor=actor,
            reason=reason,
            error_code=error_code,
        )
    )


def status_of(value: str | DownloadStatus) -> DownloadStatus:
    """Coerce a stored string back into the enum (JSON/log boundaries)."""

    if isinstance(value, DownloadStatus):
        return value
    return DownloadStatus(value)


def is_terminal(status: DownloadStatus) -> bool:
    return not ALLOWED_TRANSITIONS[status]
