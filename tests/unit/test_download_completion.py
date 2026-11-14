"""qBittorrent completion judgement — spec 14.2 / 22.1.

Only progress 1.0 **and** a finished/seeding state counts. Moving, checking and
error states must never be treated as complete.
"""

from __future__ import annotations

import pytest
from app.db.enums import DownloaderName
from app.schemas.download import (
    COMPLETE_STATES,
    FATAL_STATES,
    DownloadHandle,
    DownloadSnapshot,
    evaluate_completion,
)


def _snapshot(state: str, progress: float) -> DownloadSnapshot:
    return DownloadSnapshot(
        handle=DownloadHandle(
            downloader=DownloaderName.QBITTORRENT,
            info_hash="0" * 40,
            task_id="0" * 40,
        ),
        state=state,
        progress=progress,
        content_path="/downloads/fc2/x",
        save_path="/downloads/fc2",
    )


@pytest.mark.parametrize("state", sorted(COMPLETE_STATES))
def test_complete_states_with_full_progress(state: str) -> None:
    verdict = evaluate_completion(_snapshot(state, 1.0))
    assert verdict.complete is True
    assert verdict.fatal is False


@pytest.mark.parametrize("state", ["downloading", "stalledDL", "metaDL", "queuedDL", "pausedDL"])
def test_incomplete_states(state: str) -> None:
    verdict = evaluate_completion(_snapshot(state, 0.42))
    assert verdict.complete is False
    assert verdict.reason.startswith("progress:")


def test_progress_one_but_still_moving_is_not_complete() -> None:
    verdict = evaluate_completion(_snapshot("moving", 1.0))
    assert verdict.complete is False
    assert verdict.reason == "state_not_final:moving"


def test_progress_one_but_checking_is_not_complete() -> None:
    verdict = evaluate_completion(_snapshot("checkingResumeData", 1.0))
    assert verdict.complete is False


@pytest.mark.parametrize("state", sorted(FATAL_STATES))
def test_fatal_states(state: str) -> None:
    verdict = evaluate_completion(_snapshot(state, 1.0))
    assert verdict.complete is False
    assert verdict.fatal is True
    assert verdict.reason == f"downloader_state:{state}"


def test_unknown_state_is_not_fatal_but_not_complete() -> None:
    verdict = evaluate_completion(_snapshot("", 1.0))
    assert verdict.complete is False
    assert verdict.fatal is False


def test_float_noise_at_full_progress_is_tolerated() -> None:
    assert evaluate_completion(_snapshot("uploading", 0.9999999999)).complete is True
    assert evaluate_completion(_snapshot("uploading", 0.999)).complete is False
