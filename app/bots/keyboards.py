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


def confirm_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="确认", callback_data=f"cat:{token}:review"),
                InlineKeyboardButton(text="取消", callback_data=f"cat:{token}:cancel"),
            ]
        ]
    )
