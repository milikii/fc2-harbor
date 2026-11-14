"""Downloader-facing schemas and the shared completion judgement.

Spec references: section 14 (qBittorrent adapter, completion rules), section 15
(Transmission interface), section 17.3 (``Downloader`` protocol).

The completion rules are pure functions so they can be unit tested without a
downloader: a task is complete only when the downloader reports progress 1.0
**and** a finished/seeding state. Filesystem stability (two identical
observations) is checked separately by the organizer's stability tracker, and
moving/checking states never count as complete.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from app.db.enums import DownloaderName


class DownloaderState(StrEnum):
    """States reported by qBittorrent's ``torrents/info`` (subset we care about)."""

    ERROR = "error"
    MISSING_FILES = "missingFiles"
    UPLOADING = "uploading"
    PAUSED_UP = "pausedUP"
    STOPPED_UP = "stoppedUP"
    QUEUED_UP = "queuedUP"
    STALLED_UP = "stalledUP"
    CHECKING_UP = "checkingUP"
    FORCED_UP = "forcedUP"
    ALLOCATING = "allocating"
    DOWNLOADING = "downloading"
    META_DL = "metaDL"
    FORCED_META_DL = "forcedMetaDL"
    PAUSED_DL = "pausedDL"
    STOPPED_DL = "stoppedDL"
    QUEUED_DL = "queuedDL"
    STALLED_DL = "stalledDL"
    CHECKING_DL = "checkingDL"
    FORCED_DL = "forcedDL"
    CHECKING_RESUME_DATA = "checkingResumeData"
    MOVING = "moving"
    UNKNOWN = "unknown"


#: States in which every byte is present and the task is finished or seeding.
COMPLETE_STATES: frozenset[str] = frozenset(
    {
        DownloaderState.UPLOADING.value,
        DownloaderState.STALLED_UP.value,
        DownloaderState.PAUSED_UP.value,
        DownloaderState.STOPPED_UP.value,
        DownloaderState.QUEUED_UP.value,
        DownloaderState.FORCED_UP.value,
        DownloaderState.CHECKING_UP.value,
    }
)

#: States that will never resolve on their own: surface them instead of waiting.
FATAL_STATES: frozenset[str] = frozenset(
    {DownloaderState.ERROR.value, DownloaderState.MISSING_FILES.value}
)

#: States where the downloader is busy with data that is not final yet.
TRANSIENT_STATES: frozenset[str] = frozenset(
    {
        DownloaderState.ALLOCATING.value,
        DownloaderState.CHECKING_DL.value,
        DownloaderState.CHECKING_RESUME_DATA.value,
        DownloaderState.CHECKING_UP.value,
        DownloaderState.FORCED_META_DL.value,
        DownloaderState.META_DL.value,
        DownloaderState.MOVING.value,
    }
)

PROGRESS_EPSILON = 1e-9


@dataclass(frozen=True, slots=True)
class DownloadRequest:
    """Spec 14.1 — one magnet, one dedicated save path, one tag."""

    magnet: str
    info_hash: str
    save_path: str
    category: str
    tag: str
    job_id: str
    paused: bool = False


@dataclass(frozen=True, slots=True)
class DownloadHandle:
    """Opaque handle used to look the task up again after a restart."""

    downloader: DownloaderName
    info_hash: str
    task_id: str | None = None


@dataclass(frozen=True, slots=True)
class DownloadSnapshot:
    """A point-in-time view of one downloader task."""

    handle: DownloadHandle
    state: str
    progress: float
    name: str | None = None
    content_path: str | None = None
    save_path: str | None = None
    total_bytes: int | None = None
    downloaded_bytes: int | None = None
    extra: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CompletionVerdict:
    """Outcome of the spec 14.2 completion rules."""

    complete: bool
    reason: str
    fatal: bool = False


def evaluate_completion(snapshot: DownloadSnapshot) -> CompletionVerdict:
    """Apply spec 14.2 steps 2 and 3 (progress and finished state).

    ``fatal`` means the monitor should stop waiting and mark the job FAILED;
    the torrent task and any files stay in place.
    """

    state = (snapshot.state or "").strip()
    if state in FATAL_STATES:
        return CompletionVerdict(complete=False, reason=f"downloader_state:{state}", fatal=True)
    if snapshot.progress < 1.0 - PROGRESS_EPSILON:
        return CompletionVerdict(
            complete=False, reason=f"progress:{snapshot.progress:.4f}", fatal=False
        )
    if state not in COMPLETE_STATES:
        # progress 1.0 but e.g. "moving" / "checkingUP": not final yet.
        return CompletionVerdict(complete=False, reason=f"state_not_final:{state or 'unknown'}")
    return CompletionVerdict(complete=True, reason="complete")
