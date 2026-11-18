"""Card construction — legacy template, HTML parse mode, caption limits.

Layout (the user's baseline, taken from the legacy system)::

    <b>FC2-PPV-1234567</b>
    🏷️ 无码

    标题（≤200 字）
    💾 3.72 GiB · 🕒 2025-11-15 03:01

    📝 简介（≤250 字）
    🇨🇳 中文翻译（≤250 字）
    <a href="...">nyaa</a>
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.db.enums import Censorship
from app.services.cards import (
    CAPTION_LIMIT,
    CARD_DESCRIPTION_MAX,
    CARD_TITLE_MAX,
    PARSE_MODE,
    TEXT_LIMIT,
    CardSource,
    block_callback,
    build_card_messages,
    censorship_label,
    collapse_whitespace,
    download_callback,
    escape_html,
    format_published_at,
    parse_callback,
    split_text,
    truncate,
)

CONTENT_ID = "0b6f0d4e-0000-4000-8000-000000000001"


def card(**overrides: object) -> CardSource:
    base = {
        "content_id": CONTENT_ID,
        "canonical_key": "fc2_ppv:1234567",
        "display_code": "FC2-PPV-1234567",
        "title": "サンプル作品タイトル",
        "description_ja": "素人初撮り。19歳の大学生が人生で初めての撮影に挑戦しました。",
        "description_zh": "素人首次拍摄。19岁的大学生第一次挑战拍摄工作。",
        "censorship": Censorship.UNCENSORED,
        "nyaa_url": "https://sukebei.nyaa.si/view/4761220",
        "official_url": "https://adult.contents.fc2.com/article/1234567/",
        "size_text": "3.72 GiB",
        # 19:01 UTC is 03:01 the next day in Asia/Shanghai.
        "published_at": datetime(2025, 11, 14, 19, 1, tzinfo=UTC),
        "timezone": "Asia/Shanghai",
    }
    base.update(overrides)
    return CardSource(**base)  # type: ignore[arg-type]


def test_card_layout_matches_the_legacy_template() -> None:
    messages = build_card_messages(card())

    assert messages.extra_texts == ()
    assert messages.caption == (
        "<b>FC2-PPV-1234567</b>\n"
        "🏷️ 无码\n"
        "\n"
        "サンプル作品タイトル\n"
        "💾 3.72 GiB · 🕒 2025-11-15 03:01\n"
        "\n"
        "📝 素人初撮り。19歳の大学生が人生で初めての撮影に挑戦しました。\n"
        "🇨🇳 素人首次拍摄。19岁的大学生第一次挑战拍摄工作。\n"
        '<a href="https://sukebei.nyaa.si/view/4761220">nyaa</a>'
    )


def test_the_title_is_bilingual_when_a_translation_exists() -> None:
    caption = build_card_messages(card(title_zh="中文标题示例")).caption
    assert "中文标题示例\nサンプル作品タイトル" in caption


def test_without_a_translation_the_title_is_the_original_only() -> None:
    caption = build_card_messages(card()).caption
    assert "サンプル作品タイトル" in caption
    assert caption.splitlines()[3] == "サンプル作品タイトル"


def test_code_is_bold_on_the_first_line() -> None:
    caption = build_card_messages(card()).caption
    assert caption.splitlines()[0] == "<b>FC2-PPV-1234567</b>"


def test_tag_line_sits_on_its_own_line() -> None:
    caption = build_card_messages(card(censorship=Censorship.CENSORED)).caption
    assert caption.splitlines()[1] == "🏷️ 有码"


def test_meta_line_uses_the_dot_separator() -> None:
    caption = build_card_messages(card()).caption
    assert "💾 3.72 GiB · 🕒 2025-11-15 03:01" in caption


def test_missing_size_shows_a_dash_and_keeps_the_time() -> None:
    caption = build_card_messages(card(size_text=None)).caption
    assert "💾 - · 🕒 2025-11-15 03:01" in caption


def test_missing_time_keeps_the_size() -> None:
    caption = build_card_messages(card(published_at=None)).caption
    assert "💾 3.72 GiB" in caption
    assert "🕒" not in caption


def test_missing_size_and_time_still_render_the_line() -> None:
    caption = build_card_messages(card(size_text=None, published_at=None)).caption
    assert "💾 -" in caption
    assert "🕒" not in caption
    assert "サンプル作品タイトル\n💾 -\n" in caption


def test_link_renders_as_anchor_text() -> None:
    caption = build_card_messages(card()).caption
    assert caption.endswith('<a href="https://sukebei.nyaa.si/view/4761220">nyaa</a>')
    # Only the anchor text is visible; the bare URL never appears on its own line.
    assert "\nhttps://sukebei.nyaa.si" not in caption


def test_missing_link_drops_the_line() -> None:
    caption = build_card_messages(card(nyaa_url="")).caption
    assert "<a href" not in caption
    assert caption.endswith("🇨🇳 素人首次拍摄。19岁的大学生第一次挑战拍摄工作。")


def test_naive_timestamp_is_read_as_utc() -> None:
    # Naive on purpose: this is what SQLite hands back.
    naive = datetime(2025, 11, 14, 19, 1)
    assert format_published_at(naive) == "2025-11-15 03:01"
    assert format_published_at(None) is None


@pytest.mark.parametrize("censorship", list(Censorship))
def test_every_censorship_renders_a_label(censorship: Censorship) -> None:
    caption = build_card_messages(card(censorship=censorship)).caption
    assert f"🏷️ {censorship_label(censorship)}" in caption


def test_parse_mode_is_html() -> None:
    assert PARSE_MODE == "HTML"


# -- escaping (parse_mode=HTML) ----------------------------------------------


def test_escape_html_covers_markup_characters() -> None:
    assert escape_html("a < b & c > d") == "a &lt; b &amp; c &gt; d"


def test_title_markup_is_escaped() -> None:
    caption = build_card_messages(card(title="<script> & 100% <b>粗</b>")).caption
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption
    assert "&amp;" in caption
    # Only the code line may carry markup.
    assert caption.count("<b>") == 1
    assert caption.count("</b>") == 1


def test_description_markup_is_escaped() -> None:
    caption = build_card_messages(
        card(
            description_ja="<i>あ</i> & 説明です。とても長い説明文がここに続きます。",
            description_zh="<u>译文</u> & 内容，这是一段足够长的中文说明文字。",
        )
    ).caption
    # The translation wins (one 📝 block) and its markup is escaped.
    assert "<u>" not in caption
    assert "&lt;u&gt;译文&lt;/u&gt; &amp; 内容" in caption


def test_url_in_the_anchor_is_escaped() -> None:
    caption = build_card_messages(card(nyaa_url="https://sukebei.nyaa.si/view/1?a=1&b=2")).caption
    assert 'href="https://sukebei.nyaa.si/view/1?a=1&amp;b=2"' in caption


def test_escaped_description_still_goes_out_escaped() -> None:
    long_url = "https://sukebei.nyaa.si/view/" + "9" * 900
    messages = build_card_messages(
        card(
            description_ja="<b>あ</b> 説明です。とても長い説明文がここに続きます。",
            description_zh=None,
            nyaa_url=long_url,
        )
    )
    assert messages.has_overflow is True
    assert all("<b>" not in text for text in messages.extra_texts)
    assert messages.extra_texts[0].startswith("📝 &lt;b&gt;あ&lt;/b&gt; 説明です。")


# -- truncation --------------------------------------------------------------


def test_a_card_that_fits_is_sent_complete() -> None:
    """Spec 9.1: nothing is truncated when the whole card fits in the caption."""

    long_but_fitting = "あ" * 400
    messages = build_card_messages(card(description_ja=long_but_fitting, description_zh=None))

    assert messages.extra_texts == ()
    assert long_but_fitting in messages.caption
    assert "…" not in messages.caption


def test_overflow_caption_keeps_a_truncated_preview() -> None:
    """Not fitting means 200/250 with a visible ellipsis — not a dropped body."""

    long_ja = "あ" * 2000
    long_zh = "中" * 2000
    messages = build_card_messages(card(description_ja=long_ja, description_zh=long_zh))

    # Both lines are previewed at 250 characters — the translation is an addition.
    preview = messages.caption.split("📝 ")[1]
    japanese, chinese = preview.split("\n")[:2]
    assert len(japanese) == CARD_DESCRIPTION_MAX and japanese.endswith("…")
    assert chinese.startswith("🇨🇳 ")
    assert len(chinese.removeprefix("🇨🇳 ")) == CARD_DESCRIPTION_MAX
    assert chinese.endswith("…")
    assert len(messages.caption) <= CAPTION_LIMIT


def test_overflow_sends_the_complete_description_untruncated() -> None:
    long_ja = "あ" * 2000
    messages = build_card_messages(card(description_ja=long_ja, description_zh=None))

    assert messages.has_overflow is True
    # The caption preview is short; the message carries every character.
    assert len(messages.caption) < len(long_ja)
    assert messages.extra_texts[0] == f"📝 {long_ja}"


def test_overflow_sends_the_complete_translation_untruncated() -> None:
    long_zh = "中" * 2000
    messages = build_card_messages(card(description_ja=None, description_zh=long_zh))

    assert messages.has_overflow is True
    assert messages.extra_texts[0] == f"🇨🇳 {long_zh}"


def test_caption_title_is_truncated_when_it_does_not_fit() -> None:
    messages = build_card_messages(card(title="題" * 2000, description_ja="あ" * 2000))
    title_line = messages.caption.splitlines()[3]
    assert len(title_line) == CARD_TITLE_MAX
    assert title_line.endswith("…")


def test_whitespace_is_collapsed_in_the_description() -> None:
    caption = build_card_messages(
        card(
            description_ja="一行目の説明です。\n\n二行目   三行目の説明が続きます。",
            description_zh=None,
        )
    ).caption
    assert "📝 一行目の説明です。 二行目 三行目の説明が続きます。" in caption


def test_truncate_and_collapse_helpers() -> None:
    assert truncate("abc", 10) == "abc"
    assert truncate("abcdef", 4) == "abc…"
    assert collapse_whitespace("a \n b\tc") == "a b c"


def test_short_card_has_no_follow_up_messages() -> None:
    messages = build_card_messages(card())
    assert messages.extra_texts == ()
    assert len(messages.caption) <= CAPTION_LIMIT


def test_caption_never_exceeds_the_limit() -> None:
    long_url = "https://sukebei.nyaa.si/view/" + "9" * 900
    messages = build_card_messages(card(nyaa_url=long_url))
    assert len(messages.caption) <= CAPTION_LIMIT
    assert messages.extra_texts


def test_overflow_keeps_identity_and_link_in_the_caption() -> None:
    messages = build_card_messages(card(nyaa_url="https://sukebei.nyaa.si/view/" + "9" * 900))
    caption = messages.caption
    assert caption.startswith("<b>FC2-PPV-1234567</b>\n🏷️ 无码")
    assert caption.endswith(">nyaa</a>")


def test_overflow_sends_one_complete_body_message() -> None:
    messages = build_card_messages(card(nyaa_url="https://sukebei.nyaa.si/view/" + "9" * 900))
    assert len(messages.extra_texts) == 1
    assert messages.extra_texts[0].startswith("📝 ")


def test_japanese_and_chinese_are_both_shown() -> None:
    """The translation is an addition; the original never disappears."""

    messages = build_card_messages(
        card(
            description_ja="日本語の説明です。これは十分に長い説明文です。",
            description_zh="这是中文翻译，足够长的一段说明文字，用来通过长度门槛。",
        )
    )
    assert "📝 日本語の説明です。これは十分に長い説明文です。" in messages.caption
    assert "🇨🇳 这是中文翻译，足够长的一段说明文字，用来通过长度门槛。" in messages.caption


def test_a_non_official_description_is_labelled() -> None:
    caption = build_card_messages(card(description_official=False)).caption
    assert "上传者简介，非官方" in caption


def test_japanese_is_used_when_there_is_no_translation() -> None:
    messages = build_card_messages(
        card(description_ja="日本語だけの説明です。これは十分に長い文章です。", description_zh=None)
    )
    assert "📝 日本語だけの説明です。" in messages.caption
    assert "🇨🇳" not in messages.caption


def test_no_description_means_no_description_block() -> None:
    messages = build_card_messages(card(description_ja=None, description_zh=None))
    assert "📝" not in messages.caption
    assert "🇨🇳" not in messages.caption
    assert messages.extra_texts == ()


# -- helpers -----------------------------------------------------------------


def test_split_text_prefers_paragraph_boundaries() -> None:
    paragraphs = ["段落" * 400, "二番目" * 400, "三番目" * 400]
    text = "\n\n".join(paragraphs)
    chunks = split_text(text, limit=1200)

    assert all(len(chunk) <= 1200 for chunk in chunks)
    squash = lambda value: "".join(value.split())  # noqa: E731
    assert squash("".join(chunks)) == squash(text)


def test_extra_texts_respect_the_text_limit() -> None:
    messages = build_card_messages(card(nyaa_url="https://sukebei.nyaa.si/view/" + "9" * 900))
    assert all(len(text) <= TEXT_LIMIT for text in messages.extra_texts)


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
    download = download_callback(CONTENT_ID)
    block = block_callback(CONTENT_ID)

    assert len(download.encode()) <= 64
    assert len(block.encode()) <= 64
    assert parse_callback(download) == ("dl", CONTENT_ID)
    assert parse_callback(block) == ("blk", CONTENT_ID)
    assert parse_callback("nonsense") is None
    assert parse_callback("dl:") is None


def test_card_keyboard_has_three_buttons_in_order() -> None:
    from app.bots.keyboards import BLOCK_BUTTON, DOWNLOAD_BUTTON, OFFICIAL_BUTTON, card_keyboard

    keyboard = card_keyboard(CONTENT_ID, "https://example.com/official")
    rows = keyboard.inline_keyboard

    assert rows[0][0].text == DOWNLOAD_BUTTON == "⬇️ 下载"
    assert rows[0][1].text == BLOCK_BUTTON == "🚫 不感兴趣"
    assert rows[1][0].text == OFFICIAL_BUTTON == "🔗 FC2 官网"
    assert rows[1][0].url == "https://example.com/official"
    assert rows[0][0].callback_data.startswith("dl:")
    assert rows[0][1].callback_data.startswith("blk:")


def test_download_button_labels() -> None:
    from app.bots.subscription import DOWNLOAD_LABELS
    from app.services.acceptance import AcceptOutcome

    assert DOWNLOAD_LABELS[AcceptOutcome.SUBMITTED] == "✅ 已下载"
    assert DOWNLOAD_LABELS[AcceptOutcome.ALREADY] == "✅ 已下载"
    assert DOWNLOAD_LABELS[AcceptOutcome.FAILED] == "🔄 重试"
    assert DOWNLOAD_LABELS[AcceptOutcome.BLACKLISTED] == "已拉黑"


def test_settled_keyboard_removes_the_download_callback() -> None:
    from app.bots.keyboards import card_keyboard_after

    keyboard = card_keyboard_after(CONTENT_ID, None, download_label="已下载")
    assert keyboard.inline_keyboard[0][0].text == "已下载"
    assert keyboard.inline_keyboard[0][0].callback_data == "noop"


def test_settled_keyboard_keeps_the_official_page_button() -> None:
    """Editing a card must not silently drop the FC2 official-page button."""

    from app.bots.keyboards import card_keyboard, settled_keyboard

    original = card_keyboard(CONTENT_ID, "https://example.com/official")
    settled = settled_keyboard(original, CONTENT_ID, None, download_label="已下载")

    assert settled.inline_keyboard[0][0].text == "已下载"
    assert settled.inline_keyboard[0][0].callback_data == "noop"
    assert settled.inline_keyboard[0][1].text == "🚫 不感兴趣"
    assert settled.inline_keyboard[1][0].url == "https://example.com/official"


def test_settled_keyboard_without_markup_rebuilds_a_usable_card() -> None:
    from app.bots.keyboards import settled_keyboard

    settled = settled_keyboard(
        None, CONTENT_ID, "https://example.com/official", download_label="重试"
    )
    assert settled.inline_keyboard[0][0].text == "重试"
    assert settled.inline_keyboard[1][0].url == "https://example.com/official"


def test_a_real_length_description_takes_the_follow_up_path() -> None:
    """A 1673-character FC2 description (a real page) must not be cut to a preview."""

    real_length = "あ" * 1673
    messages = build_card_messages(card(description_ja=real_length, description_zh=None))

    assert messages.has_overflow is True
    assert len(messages.caption) <= CAPTION_LIMIT
    assert messages.extra_texts[0] == f"📝 {real_length}"


# -- the title never repeats the code ----------------------------------------


@pytest.mark.parametrize(
    "raw_title",
    [
        "FC2-PPV-1234567 作品タイトル",
        "FC2-1234567 作品タイトル",
        "FC2PPV1234567 作品タイトル",
        "FC2 PPV 1234567 作品タイトル",
        "fc2_ppv_1234567 作品タイトル",
        "fc2.ppv.01234567 作品タイトル",
        "+++ FC2-PPV-1234567 作品タイトル",
    ],
)
def test_the_code_is_stripped_from_the_title(raw_title: str) -> None:
    caption = build_card_messages(card(title=raw_title)).caption
    title_line = caption.splitlines()[3]

    assert title_line == "作品タイトル"


def test_a_title_that_is_only_the_code_falls_back_to_the_display_code() -> None:
    caption = build_card_messages(card(title="FC2-PPV-1234567")).caption
    assert caption.splitlines()[3] == "FC2-PPV-1234567"


def test_a_missing_title_falls_back_to_the_display_code() -> None:
    caption = build_card_messages(card(title=None)).caption
    assert caption.splitlines()[3] == "FC2-PPV-1234567"


def test_the_title_is_truncated_to_two_hundred_characters() -> None:
    caption = build_card_messages(card(title="題" * 2000, description_zh=None)).caption
    title_line = caption.splitlines()[3]
    assert len(title_line) == CARD_TITLE_MAX
    assert title_line.endswith("…")


def test_the_description_is_truncated_to_two_hundred_fifty_characters() -> None:
    caption = build_card_messages(
        card(description_ja="あ" * 2000, description_zh="中" * 2000)
    ).caption
    japanese, chinese = caption.split("📝 ")[1].split("\n")[:2]
    assert len(japanese) == CARD_DESCRIPTION_MAX and japanese.endswith("…")
    assert len(chinese.removeprefix("🇨🇳 ")) == CARD_DESCRIPTION_MAX
    assert chinese.endswith("…")


def test_the_size_falls_back_to_a_dash() -> None:
    caption = build_card_messages(card(size_text=None)).caption
    assert "💾 - · 🕒" in caption


# -- 📝 junk filter ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "https://imagetwist.com/367wmg0gouoq/grewag.png",
        "https://a.com/1.jpg https://a.com/2.jpg https://a.com/3.jpg",
        "大家好，这里是【人渣崩坏】官方频道↓请在此加载我们会不定期更新信息",
        "★ 特価★★ 3960pt→1980pt",
        "高画質版はこちらからどうぞ、ぜひご登録ください。",
        "短い。",
    ],
)
def test_junk_descriptions_are_dropped(text: str) -> None:
    from app.services.cards import usable_description

    assert usable_description(text) is None


def test_a_real_description_survives_the_filter() -> None:
    from app.services.cards import usable_description

    text = "素人初撮り。19歳の大学生が人生で初めての撮影に挑戦しました。"
    assert usable_description(text) == text


def test_the_card_omits_a_junk_description() -> None:
    messages = build_card_messages(
        card(description_ja="https://imagetwist.com/367wmg0gouoq/grewag.png", description_zh=None)
    )
    assert "📝" not in messages.caption
    assert "imagetwist" not in messages.caption


def test_a_short_title_is_never_truncated() -> None:
    """A 46-character title stays 46: only >200 characters are cut."""

    short = "極秘映像七沢あ" + "い" * 39
    assert len(short) == 46
    caption = build_card_messages(card(title=short, description_zh=None)).caption
    assert caption.splitlines()[3] == short
    assert "…" not in caption.splitlines()[3]
