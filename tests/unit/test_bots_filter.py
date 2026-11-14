"""Bot whitelist filter — spec 20 (every handler re-checks identity)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.bots.commands import WhitelistFilter


def message(user_id: int, chat_type: str = "private"):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        chat=SimpleNamespace(type=chat_type),
    )


async def test_allowed_private_user_passes() -> None:
    assert await WhitelistFilter([111, 222])(message(111)) is True


async def test_unknown_user_is_rejected() -> None:
    assert await WhitelistFilter([111])(message(999)) is False


async def test_missing_user_is_rejected() -> None:
    event = SimpleNamespace(from_user=None, chat=SimpleNamespace(type="private"))
    assert await WhitelistFilter([111])(event) is False


@pytest.mark.parametrize("chat_type", ["group", "supergroup", "channel"])
async def test_non_private_chat_is_rejected(chat_type: str) -> None:
    assert await WhitelistFilter([111])(message(111, chat_type)) is False
