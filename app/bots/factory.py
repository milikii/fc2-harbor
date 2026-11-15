"""Bot construction and identity check — spec 8.2 / 18.

Two things used to be missing here, both invisible in a healthy-looking container:

* ``telegram.proxy`` was validated but never given to the Bot, so aiogram used its
  default session with no proxy. On a filtered network that means no connection at
  all while the process stays up.
* nothing ever asked Telegram "am I really connected?", so a dead Bot looked exactly
  like an idle one. :func:`verify_bot` calls ``getMe`` once at startup.

The proxy profile is resolved through the same :class:`ProxyRouter` as everything
else, so ``direct`` really means direct.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import Settings
from app.logging import error_detail, get_logger
from app.network.proxy import ProxyRouter

_log = get_logger("telegram.bot")

DEFAULT_GETME_TIMEOUT = 10.0


@dataclass(frozen=True, slots=True)
class BotIdentity:
    name: str
    telegram_id: int
    username: str | None


def aiogram_proxy_url(url: str | None) -> str | None:
    """Translate a proxy URL into the form ``aiohttp-socks`` accepts.

    ``socks5h`` is the "resolve DNS at the proxy" scheme used everywhere else in
    this project (and by httpx), but python-socks only knows ``socks5``; its SOCKS5
    connector resolves hostnames remotely by default, so the behaviour is the same.
    """

    if url and url.startswith("socks5h://"):
        return "socks5://" + url[len("socks5h://") :]
    return url


def resolve_telegram_proxy(settings: Settings) -> str | None:
    """Proxy URL for both Bots, or ``None`` for a direct connection."""

    router = ProxyRouter(settings.proxy_profiles)
    return router.target(settings.telegram.proxy).url


def build_bot(token: str, settings: Settings, *, name: str = "bot") -> Bot:
    """Create a Bot whose session uses ``telegram.proxy``.

    ``AiohttpSession(proxy=...)`` needs ``aiohttp-socks``, which is a declared
    dependency; a socks5h or http profile both work through it.
    """

    proxy_url = aiogram_proxy_url(resolve_telegram_proxy(settings))
    if proxy_url is None:
        _log.info("telegram.bot_created", bot=name, proxy="direct")
        return Bot(token=token)

    _log.info("telegram.bot_created", bot=name, proxy_profile=settings.telegram.proxy)
    return Bot(token=token, session=AiohttpSession(proxy=proxy_url))


async def verify_bot(
    bot: Bot, *, name: str, timeout_seconds: float = DEFAULT_GETME_TIMEOUT
) -> BotIdentity | None:
    """Call ``getMe`` once; a failure is loud, because silence is the real danger."""

    try:
        me = await asyncio.wait_for(bot.get_me(), timeout=timeout_seconds)
    except TimeoutError:
        _log.error(
            "telegram.bot_unreachable",
            bot=name,
            error_code="timeout",
            hint="Bot cannot reach Telegram: check telegram.proxy and network",
        )
        return None
    except Exception as exc:
        _log.error(
            "telegram.bot_unreachable",
            bot=name,
            error_code=type(exc).__name__,
            error_detail=error_detail(exc),
            hint="Bot cannot reach Telegram: check telegram.proxy and network",
        )
        return None

    identity = BotIdentity(
        name=name, telegram_id=int(getattr(me, "id", 0)), username=getattr(me, "username", None)
    )
    _log.info(
        "telegram.bot_identity",
        bot=name,
        username=identity.username,
        telegram_id=identity.telegram_id,
    )
    return identity
