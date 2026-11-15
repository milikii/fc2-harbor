"""Which supervised tasks startup creates — spec 6.2 / 10.

With ``subscription.polling_enabled: false`` the poller must not be started at all,
so the manual bot can run without touching Nyaa.
"""

from __future__ import annotations

from pathlib import Path

from app.main import planned_tasks

from tests.support import make_settings


def test_default_plan_polls_and_pushes(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"subscription_bot_token": "123456:SUB"})
    assert planned_tasks(settings) == [
        "health-server",
        "manual-bot",
        "monitor",
        "subscription-poll",
        "subscription-bot",
        "publisher",
        "message-deleter",
    ]


def test_polling_disabled_removes_the_subscription_task(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        telegram={"subscription_bot_token": "123456:SUB"},
        subscription={"polling_enabled": False},
    )
    tasks = planned_tasks(settings)

    assert "subscription-poll" not in tasks
    assert "subscription-bot" in tasks  # card callbacks still need polling
    assert "publisher" in tasks  # push still works for already discovered content
    assert "manual-bot" in tasks


def test_no_subscription_token_means_no_publisher(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"subscription_bot_token": None})

    tasks = planned_tasks(settings)
    assert "publisher" not in tasks
    assert "message-deleter" not in tasks
    assert "subscription-bot" not in tasks
    assert "subscription-poll" in tasks  # polling still records discoveries
