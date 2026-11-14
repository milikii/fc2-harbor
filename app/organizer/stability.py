"""File stability check — spec 14.2 steps 5 and 6.

A download is only considered finished when the *filesystem* view has stopped
changing: file count and total size must be identical across two consecutive
observations, ``stability_interval_seconds`` apart. qBittorrent temp files
(``.!qB`` / ``.parts``) are excluded so they cannot mask a still-running write.

The tracker keeps state per job in memory, which is enough: after a restart the
monitor simply observes twice again. Nothing is moved before the check passes.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from app.logging import get_logger
from app.organizer.mover import is_temp_file

_log = get_logger("organizer.stability")

DEFAULT_REQUIRED_CHECKS: Final[int] = 2


@dataclass(frozen=True, slots=True)
class PathSignature:
    file_count: int
    total_bytes: int


@dataclass(frozen=True, slots=True)
class StabilityVerdict:
    stable: bool
    reason: str
    signature: PathSignature
    checks: int
    waited_seconds: float
    timed_out: bool = False


def snapshot_path(path: Path) -> PathSignature:
    """Count regular, non-temp files and sum their sizes."""

    if path.is_file():
        return PathSignature(file_count=1, total_bytes=path.stat().st_size)
    if not path.is_dir():
        raise FileNotFoundError(f"download path does not exist: {path}")

    count = 0
    total = 0
    for entry in path.rglob("*"):
        if entry.is_symlink() or not entry.is_file() or is_temp_file(entry):
            continue
        count += 1
        total += entry.stat().st_size
    return PathSignature(file_count=count, total_bytes=total)


@dataclass(slots=True)
class _State:
    signature: PathSignature
    first_seen: float
    checks: int


class StabilityTracker:
    """Per-job "two identical observations" tracker."""

    def __init__(
        self,
        *,
        required_checks: int = DEFAULT_REQUIRED_CHECKS,
        interval_seconds: float = 10.0,
        max_wait_seconds: float = 600.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if required_checks < 2:
            raise ValueError("required_checks must be at least 2 (spec 14.2)")
        self.required_checks = required_checks
        self.interval_seconds = interval_seconds
        self.max_wait_seconds = max_wait_seconds
        self._clock = clock or time.monotonic
        self._states: dict[str, _State] = {}

    def observe(self, key: str, path: Path) -> StabilityVerdict:
        now = self._clock()
        signature = snapshot_path(path)
        state = self._states.get(key)

        if state is None or state.signature != signature:
            if state is not None:
                _log.info(
                    "stability.changed",
                    job_id=key,
                    file_count=signature.file_count,
                    bytes=signature.total_bytes,
                )
            self._states[key] = _State(signature=signature, first_seen=now, checks=1)
            return StabilityVerdict(
                stable=False,
                reason="first_observation",
                signature=signature,
                checks=1,
                waited_seconds=0.0,
            )

        state.checks += 1
        waited = now - state.first_seen
        if waited > self.max_wait_seconds:
            _log.warning(
                "stability.timeout",
                job_id=key,
                waited_seconds=round(waited, 1),
                checks=state.checks,
            )
            return StabilityVerdict(
                stable=False,
                reason="stability_timeout",
                signature=signature,
                checks=state.checks,
                waited_seconds=waited,
                timed_out=True,
            )

        enough_checks = state.checks >= self.required_checks
        enough_time = waited >= self.interval_seconds * (self.required_checks - 1)
        if enough_checks and enough_time:
            _log.info(
                "stability.confirmed",
                job_id=key,
                file_count=signature.file_count,
                bytes=signature.total_bytes,
                checks=state.checks,
                waited_seconds=round(waited, 1),
            )
            return StabilityVerdict(
                stable=True,
                reason="stable",
                signature=signature,
                checks=state.checks,
                waited_seconds=waited,
            )

        return StabilityVerdict(
            stable=False,
            reason="waiting_for_interval",
            signature=signature,
            checks=state.checks,
            waited_seconds=waited,
        )

    def forget(self, key: str) -> None:
        self._states.pop(key, None)

    def reset(self) -> None:
        self._states.clear()
