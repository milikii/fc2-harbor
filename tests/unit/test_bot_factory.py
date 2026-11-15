"""Bot construction and the startup identity check — spec 8.2 / 18.

Two deployment failures this covers: ``telegram.proxy`` was validated but never
handed to the Bot (so a filtered network meant no connection at all), and nothing
ever asked Telegram whether the Bot was actually reachable (so a dead Bot looked
idle). The proxy goes through ``AiohttpSession``, which needs ``aiohttp-socks``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.bots.factory import (
    aiogram_proxy_url,
    build_bot,
    resolve_telegram_proxy,
    verify_bot,
)

from tests.support import make_settings


def settings_with_proxy(tmp_path: Path):
    return make_settings(tmp_path, telegram={"proxy": "japan"})


def test_direct_telegram_means_no_proxy(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"proxy": "direct"})
    assert resolve_telegram_proxy(settings) is None


def test_named_profile_resolves_to_its_url(tmp_path: Path) -> None:
    settings = settings_with_proxy(tmp_path)
    assert resolve_telegram_proxy(settings) == "socks5h://user:password@127.0.0.1:1080"


def test_socks5h_is_translated_for_aiohttp_socks() -> None:
    """python-socks only accepts socks5://; it resolves DNS remotely anyway."""

    assert (
        aiogram_proxy_url("socks5h://user:password@127.0.0.1:1080")
        == "socks5://user:password@127.0.0.1:1080"
    )
    assert aiogram_proxy_url("http://127.0.0.1:7890") == "http://127.0.0.1:7890"
    assert aiogram_proxy_url(None) is None


def test_bot_session_carries_the_configured_proxy(tmp_path: Path) -> None:
    bot = build_bot("123456:TEST", settings_with_proxy(tmp_path), name="subscription-bot")
    try:
        assert bot.session._proxy == "socks5://user:password@127.0.0.1:1080"
    finally:
        asyncio.run(bot.session.close())


def test_bot_session_is_plain_when_direct(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"proxy": "direct"})
    bot = build_bot("123456:TEST", settings, name="manual-bot")
    try:
        assert bot.session._proxy is None
    finally:
        asyncio.run(bot.session.close())


class FakeBot:
    def __init__(self, *, fail: bool = False, delay: float = 0.0) -> None:
        self.fail = fail
        self.delay = delay

    async def get_me(self):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("Network is unreachable")
        return SimpleNamespace(id=4242, username="fc2_harbor_bot")


async def test_identity_is_reported_on_success() -> None:
    identity = await verify_bot(FakeBot(), name="manual-bot", timeout_seconds=1)

    assert identity is not None
    assert identity.telegram_id == 4242
    assert identity.username == "fc2_harbor_bot"
    assert identity.name == "manual-bot"


async def test_unreachable_bot_returns_none() -> None:
    assert await verify_bot(FakeBot(fail=True), name="manual-bot", timeout_seconds=1) is None


async def test_slow_bot_times_out() -> None:
    assert await verify_bot(FakeBot(delay=1), name="manual-bot", timeout_seconds=0.01) is None
