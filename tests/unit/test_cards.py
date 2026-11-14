"""Card construction — spec 9.1 / 9.2 (caption limits, no silent truncation)."""

from __future__ import annotations

import pytest
from app.db.enums import Censorship
from app.services.cards import (
    CAPTION_LIMIT,
    TEXT_LIMIT,
    CardSource,
    block_callback,
    build_card_messages,
    censorship_label,
    download_callback,
    parse_callback,
    split_text,
)


def card(**overrides: object) -> CardSource:
    base = {
        "content_id": "0b6f0d4e-0000-4000-8000-000000000001",
        "canonical_key": "fc2_ppv:1234567",
        "display_code": "FC2-PPV-1234567",
        "title": "サンプル作品タイトル",
        "description_ja": "短い説明。",
        "description_zh": "简短说明。",
        "censorship": Censorship.UNCENSORED,
        "nyaa_url": "https://sukebei.nyaa.si/view/4761220",
        "official_url": "https://adult.contents.fc2.com/article/1234567/",
    }
    base.update(overrides)
    return CardSource(**base)  # type: ignore[arg-type]


def test_short_description_stays_in_the_caption() -> None:
    messages = build_card_messages(card())

    assert messages.extra_texts == ()
    assert "无码" in messages.caption
    assert "FC2-PPV-1234567" in messages.caption
    assert "https://sukebei.nyaa.si/view/4761220" in messages.caption
    assert "短い説明。" in messages.caption
    assert "简短说明。" in messages.caption
    assert len(messages.caption) <= CAPTION_LIMIT


def test_long_description_moves_to_follow_up_messages_without_loss() -> None:
    long_ja = "あ" * 1500
    messages = build_card_messages(card(description_ja=long_ja, description_zh=None))

    assert messages.has_overflow is True
    assert messages.caption.startswith("无码｜サンプル作品タイトル")
    assert "FC2-PPV-1234567" in messages.caption
    assert "https://sukebei.nyaa.si/view/4761220" in messages.caption
    assert long_ja not in messages.caption
    assert "".join(messages.extra_texts) == long_ja
    assert all(len(text) <= TEXT_LIMIT for text in messages.extra_texts)


def test_caption_never_exceeds_the_limit() -> None:
    messages = build_card_messages(card(description_ja="い" * 5000, description_zh="中" * 5000))
    assert len(messages.caption) <= CAPTION_LIMIT
    assert messages.extra_texts


def test_split_text_prefers_paragraph_boundaries() -> None:
    paragraphs = ["段落" * 400, "二番目" * 400, "三番目" * 400]
    text = "\n\n".join(paragraphs)
    chunks = split_text(text, limit=1200)

    assert all(len(chunk) <= 1200 for chunk in chunks)
    # Only boundary whitespace may disappear; every character is preserved.
    squash = lambda value: "".join(value.split())  # noqa: E731
    assert squash("".join(chunks)) == squash(text)


def test_empty_body_still_produces_a_caption() -> None:
    messages = build_card_messages(card(description_ja=None, description_zh=None))
    assert messages.caption
    assert messages.extra_texts == ()


@pytest.mark.parametrize(
    ("censorship", "label"),
    [
        (Censorship.CENSORED, "有码"),
        (Censorship.UNCENSORED, "无码"),
        (Censorship.THIN, "薄码"),
        (Censorship.UNKNOWN, "未知"),
    ],
)
def test_censorship_labels(censorship: Censorship, label: str) -> None:
    assert censorship_label(censorship) == label


def test_callback_data_is_short_and_round_trips() -> None:
    content_id = "0b6f0d4e-0000-4000-8000-000000000001"
    download = download_callback(content_id)
    block = block_callback(content_id)

    assert len(download.encode()) <= 64
    assert len(block.encode()) <= 64
    assert parse_callback(download) == ("dl", content_id)
    assert parse_callback(block) == ("blk", content_id)
    assert parse_callback("nonsense") is None
    assert parse_callback("dl:") is None


def test_card_keyboard_has_three_buttons_in_order() -> None:
    from app.bots.keyboards import card_keyboard

    keyboard = card_keyboard("0b6f0d4e-0000-4000-8000-000000000001", "https://example.com/official")
    rows = keyboard.inline_keyboard

    assert rows[0][0].text == "下载"
    assert rows[0][1].text == "不感兴趣"
    assert rows[1][0].url == "https://example.com/official"
    assert rows[0][0].callback_data.startswith("dl:")
    assert rows[0][1].callback_data.startswith("blk:")


def test_download_button_labels() -> None:
    from app.bots.subscription import DOWNLOAD_LABELS
    from app.services.acceptance import AcceptOutcome

    assert DOWNLOAD_LABELS[AcceptOutcome.SUBMITTED] == "已下载"
    assert DOWNLOAD_LABELS[AcceptOutcome.ALREADY] == "已下载"
    assert DOWNLOAD_LABELS[AcceptOutcome.FAILED] == "重试"
    assert DOWNLOAD_LABELS[AcceptOutcome.BLACKLISTED] == "已拉黑"


def test_settled_keyboard_removes_the_download_callback() -> None:
    from app.bots.keyboards import card_keyboard_after

    keyboard = card_keyboard_after(
        "0b6f0d4e-0000-4000-8000-000000000001", None, download_label="已下载"
    )
    assert keyboard.inline_keyboard[0][0].text == "已下载"
    assert keyboard.inline_keyboard[0][0].callback_data == "noop"
