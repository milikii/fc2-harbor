"""Background task supervision — a dead task must never look like a healthy one.

The polling tasks used to be plain ``create_task`` calls whose references were held
in a list while the main coroutine waited on a stop event. If one of them raised,
nothing observed it: the container stayed healthy, the logs stayed clean and the
system simply stopped doing its job. This supervisor wraps every task, logs the
failure with the task name, restarts it with exponential backoff a bounded number of
times, and exposes ``running / restarting / dead`` to ``/status``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from app.logging import error_detail, get_logger

_log = get_logger("supervisor")

DEFAULT_MAX_RESTARTS = 3
DEFAULT_BACKOFF_SECONDS = 5.0
MAX_BACKOFF_SECONDS = 60.0

RUNNING = "running"
RESTARTING = "restarting"
DEAD = "dead"
STOPPED = "stopped"


@dataclass(slots=True)
class TaskRecord:
    name: str
    state: str = RUNNING
    restarts: int = 0
    last_error: str | None = None


@dataclass
class TaskSupervisor:
    max_restarts: int = DEFAULT_MAX_RESTARTS
    backoff_seconds: float = DEFAULT_BACKOFF_SECONDS
    max_backoff_seconds: float = MAX_BACKOFF_SECONDS
    records: dict[str, TaskRecord] = field(default_factory=dict)
    tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    _stopping: bool = False
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event)

    # -- spawning -----------------------------------------------------------

    def spawn(self, name: str, factory: Callable[[], Awaitable[None]]) -> asyncio.Task[None]:
        """Start ``factory`` under supervision; returns the wrapping task."""

        record = TaskRecord(name=name)
        self.records[name] = record
        task = asyncio.create_task(self._supervise(record, factory), name=name)
        self.tasks[name] = task
        return task

    async def _supervise(self, record: TaskRecord, factory: Callable[[], Awaitable[None]]) -> None:
        while True:
            try:
                record.state = RUNNING
                await factory()
                # A task that returns normally has finished its job (e.g. the health
                # server after should_exit).
                record.state = STOPPED
                return
            except asyncio.CancelledError:
                record.state = STOPPED
                raise
            except Exception as exc:
                record.last_error = f"{type(exc).__name__}: {exc}"
                _log.error(
                    "supervisor.task_failed",
                    task=record.name,
                    error_code=type(exc).__name__,
                    error_detail=error_detail(exc),
                    restarts=record.restarts,
                )
                if self._stopping or record.restarts >= self.max_restarts:
                    record.state = DEAD
                    _log.error(
                        "supervisor.task_dead",
                        task=record.name,
                        restarts=record.restarts,
                        last_error=record.last_error,
                    )
                    return

                record.restarts += 1
                record.state = RESTARTING
                delay = min(
                    self.backoff_seconds * (2 ** (record.restarts - 1)),
                    self.max_backoff_seconds,
                )
                _log.warning(
                    "supervisor.task_restart",
                    task=record.name,
                    restart=record.restarts,
                    delay_seconds=delay,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
                except TimeoutError:
                    continue
                record.state = STOPPED
                return

    # -- shutdown -----------------------------------------------------------

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        for task in self.tasks.values():
            task.cancel()
        for name, task in self.tasks.items():
            try:
                await task
            except asyncio.CancelledError:
                continue
            except Exception as exc:  # shutdown must not raise
                _log.warning(
                    "supervisor.task_stop_failed",
                    task=name,
                    error_code=type(exc).__name__,
                )

    # -- reporting ----------------------------------------------------------

    def snapshot(self) -> dict[str, str]:
        return {name: record.state for name, record in sorted(self.records.items())}

    def dead_tasks(self) -> list[str]:
        return [name for name, record in self.records.items() if record.state == DEAD]

    def errors(self) -> dict[str, str | None]:
        return {
            name: record.last_error for name, record in self.records.items() if record.last_error
        }

    def running(self) -> bool:
        return not self.dead_tasks()
