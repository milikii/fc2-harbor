"""File stability tracking — spec 14.2 (two identical observations, temp files excluded)."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.organizer.stability import StabilityTracker, snapshot_path


class FakeClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_two_identical_observations_are_required(tmp_path: Path) -> None:
    clock = FakeClock()
    tracker = StabilityTracker(required_checks=2, interval_seconds=10, clock=clock)
    target = tmp_path / "download"
    target.mkdir()
    (target / "a.mkv").write_bytes(b"a" * 100)

    first = tracker.observe("job-1", target)
    assert first.stable is False
    assert first.reason == "first_observation"

    clock.advance(9)
    second = tracker.observe("job-1", target)
    assert second.stable is False
    assert second.reason == "waiting_for_interval"

    clock.advance(1)
    third = tracker.observe("job-1", target)
    assert third.stable is True
    assert third.signature.file_count == 1
    assert third.signature.total_bytes == 100


def test_growing_file_resets_the_tracker(tmp_path: Path) -> None:
    clock = FakeClock()
    tracker = StabilityTracker(required_checks=2, interval_seconds=10, clock=clock)
    target = tmp_path / "download"
    target.mkdir()
    file = target / "a.mkv"
    file.write_bytes(b"a" * 100)

    tracker.observe("job-1", target)
    clock.advance(10)
    assert tracker.observe("job-1", target).stable is True

    with open(file, "ab") as handle:
        handle.write(b"more")
    clock.advance(10)
    changed = tracker.observe("job-1", target)
    assert changed.stable is False
    assert changed.reason == "first_observation"


def test_temp_files_are_ignored(tmp_path: Path) -> None:
    target = tmp_path / "download"
    target.mkdir()
    (target / "a.mkv").write_bytes(b"a" * 10)
    (target / "a.mkv.!qB").write_bytes(b"x" * 500)
    (target / "b.parts").write_bytes(b"y" * 500)

    signature = snapshot_path(target)
    assert signature.file_count == 1
    assert signature.total_bytes == 10


def test_single_file_path(tmp_path: Path) -> None:
    file = tmp_path / "single.mkv"
    file.write_bytes(b"12345")
    signature = snapshot_path(file)
    assert (signature.file_count, signature.total_bytes) == (1, 5)


def test_timeout_is_reported(tmp_path: Path) -> None:
    clock = FakeClock()
    tracker = StabilityTracker(
        required_checks=2, interval_seconds=100, max_wait_seconds=5, clock=clock
    )
    target = tmp_path / "download"
    target.mkdir()
    (target / "a.mkv").write_bytes(b"a")

    tracker.observe("job-1", target)
    clock.advance(6)
    verdict = tracker.observe("job-1", target)
    assert verdict.stable is False
    assert verdict.timed_out is True
    assert verdict.reason == "stability_timeout"


def test_missing_path_raises(tmp_path: Path) -> None:
    tracker = StabilityTracker(clock=FakeClock())
    with pytest.raises(FileNotFoundError):
        tracker.observe("job-1", tmp_path / "missing")


def test_forget_and_reset(tmp_path: Path) -> None:
    clock = FakeClock()
    tracker = StabilityTracker(required_checks=2, interval_seconds=1, clock=clock)
    target = tmp_path / "download"
    target.mkdir()
    (target / "a.mkv").write_bytes(b"a")

    tracker.observe("job-1", target)
    tracker.forget("job-1")
    clock.advance(5)
    assert tracker.observe("job-1", target).reason == "first_observation"


def test_required_checks_must_be_at_least_two() -> None:
    with pytest.raises(ValueError):
        StabilityTracker(required_checks=1)
