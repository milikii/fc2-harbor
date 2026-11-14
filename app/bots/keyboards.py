"""Inline keyboards for the manual bot (spec 10.8).

Callback payloads stay short: ``cat:<token>:<category>`` with an 8 character
token, far below Telegram's 64-byte limit. The magnet never travels in a
callback (spec 9.2).
"""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.manual_ingest import CATEGORY_LABELS

CATEGORY_ORDER: tuple[str, ...] = ("fc2", "jav", "cn", "western", "review")


def category_keyboard(token: str) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for category in CATEGORY_ORDER:
        row.append(
            InlineKeyboardButton(
                text=CATEGORY_LABELS.get(category, category),
                callback_data=f"cat:{token}:{category}",
            )
        )
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="取消", callback_data=f"cat:{token}:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard(
    content_id: str, official_url: str | None, *, download_label: str = "下载"
) -> InlineKeyboardMarkup:
    """Spec 9.2: download, not interested, then the FC2 official page."""

    from app.services.cards import block_callback, download_callback

    rows: list[list[InlineKeyboardButton]] = [
        [
            InlineKeyboardButton(text=download_label, callback_data=download_callback(content_id)),
            InlineKeyboardButton(text="不感兴趣", callback_data=block_callback(content_id)),
        ]
    ]
    if official_url:
        rows.append([InlineKeyboardButton(text="FC2 官网", url=official_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def card_keyboard_after(
    content_id: str, official_url: str | None, *, download_label: str
) -> InlineKeyboardMarkup:
    """The same card with a settled download button (已下载 / 重试 / 已拉黑)."""

    keyboard = card_keyboard(content_id, official_url, download_label=download_label)
    rows = list(keyboard.inline_keyboard)
    rows[0][0] = InlineKeyboardButton(text=download_label, callback_data="noop")
    return InlineKeyboardMarkup(inline_keyboard=rows)


def confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="确认", callback_data=f"cat:{token}:review"),
                InlineKeyboardButton(text="取消", callback_data=f"cat:{token}:cancel"),
            ]
        ]
    )
