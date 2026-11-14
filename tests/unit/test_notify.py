"""Admin notifications — spec 16.3 (failures must reach the administrator).

Uses a fake Telegram bot: no network, and a broken bot must never break a job.
"""

from __future__ import annotations

from app.services.notify import TelegramAdminNotifier

from tests.support import FakeTelegramBot


async def test_notice_is_sent_to_every_admin() -> None:
    bot = FakeTelegramBot()
    notifier = TelegramAdminNotifier(bot, [111, 222, 111])

    await notifier.notify("整理失败 job=1")

    assert [chat_id for chat_id, _ in bot.sent] == [111, 222]
    assert "整理失败" in bot.sent[0][1]


async def test_telegram_failure_is_swallowed() -> None:
    bot = FakeTelegramBot(fail=True)
    notifier = TelegramAdminNotifier(bot, [111])

    # Must not raise: a notification failure cannot fail the job.
    await notifier.notify("boom")
    assert bot.sent == []


async def test_long_message_is_truncated() -> None:
    bot = FakeTelegramBot()
    notifier = TelegramAdminNotifier(bot, [111])

    await notifier.notify("x" * 9000)

    assert len(bot.sent[0][1]) <= 4000
