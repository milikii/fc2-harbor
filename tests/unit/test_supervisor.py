"""Task supervision — a dead background task must be visible, never silent.

The polling tasks used to be plain ``create_task`` calls whose exception nobody
retrieved, so a crashed Bot looked exactly like an idle one.
"""

from __future__ import annotations

import asyncio

import pytest
from app.services.supervisor import DEAD, RUNNING, STOPPED, TaskSupervisor


async def test_failing_task_becomes_dead_and_keeps_the_error() -> None:
    supervisor = TaskSupervisor(max_restarts=0)

    async def boom() -> None:
        raise RuntimeError("telegram unreachable")

    supervisor.spawn("subscription-bot", boom)
    await supervisor.tasks["subscription-bot"]

    assert supervisor.snapshot() == {"subscription-bot": DEAD}
    assert supervisor.dead_tasks() == ["subscription-bot"]
    assert "telegram unreachable" in (supervisor.errors()["subscription-bot"] or "")
    assert supervisor.running() is False


async def test_task_is_restarted_with_a_counter() -> None:
    supervisor = TaskSupervisor(max_restarts=2, backoff_seconds=0)
    attempts = {"n": 0}

    async def flaky() -> None:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("first attempt fails")
        await asyncio.sleep(3600)

    supervisor.spawn("monitor", flaky)
    await asyncio.sleep(0.05)

    assert attempts["n"] == 2
    assert supervisor.records["monitor"].restarts == 1
    assert supervisor.snapshot()["monitor"] == RUNNING
    await supervisor.stop()


async def test_restarts_are_bounded() -> None:
    supervisor = TaskSupervisor(max_restarts=1, backoff_seconds=0)

    async def always_fails() -> None:
        raise RuntimeError("always")

    supervisor.spawn("publisher", always_fails)
    await supervisor.tasks["publisher"]

    assert supervisor.snapshot()["publisher"] == DEAD
    assert supervisor.records["publisher"].restarts == 1


async def test_stop_cancels_and_marks_stopped() -> None:
    supervisor = TaskSupervisor()

    async def forever() -> None:
        await asyncio.sleep(3600)

    supervisor.spawn("health-server", forever)
    await asyncio.sleep(0)
    assert supervisor.snapshot()["health-server"] == RUNNING

    await supervisor.stop()
    assert supervisor.snapshot()["health-server"] == STOPPED


async def test_normal_return_is_stopped_not_dead() -> None:
    supervisor = TaskSupervisor()

    async def finished() -> None:
        return None

    supervisor.spawn("health-server", finished)
    await supervisor.tasks["health-server"]

    assert supervisor.snapshot()["health-server"] == STOPPED
    assert supervisor.dead_tasks() == []


@pytest.mark.parametrize("name", ["manual-bot", "subscription-bot", "monitor", "publisher"])
async def test_each_task_is_tracked_by_name(name: str) -> None:
    supervisor = TaskSupervisor()

    async def forever() -> None:
        await asyncio.sleep(3600)

    supervisor.spawn(name, forever)
    await asyncio.sleep(0)

    assert supervisor.snapshot() == {name: RUNNING}
    await supervisor.stop()
