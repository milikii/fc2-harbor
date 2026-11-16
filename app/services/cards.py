"""Telegram card construction — spec 9.1 / 9.2.

Two hard rules from the spec:

* the first message is a photo whose caption carries tag, title, FC2 code and the
  Nyaa link; if the description does not fit in a caption it is sent as following
  text messages — the body is **never** silently truncated;
* ``callback_data`` stays short (``dl:<uuid>`` / ``blk:<uuid>``), never a magnet or
  a URL, and always below Telegram's 64-byte limit.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from app.db.enums import Censorship

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
#: Room left for the ellipsis/separators when packing caption text.
CAPTION_SAFETY = 64

#: Card layout, copied from the legacy system's template (the user's baseline).
#: HTML parse mode, so every user-supplied string is escaped.
PARSE_MODE = "HTML"
#: Caps used *only* when the whole card does not fit in a caption. The complete
#: description and translation still go out as follow-up text messages, so these
#: limits never lose text — they only keep the caption readable.
CARD_TITLE_MAX = 200
CARD_DESCRIPTION_MAX = 250
CODE_LINE_TEMPLATE = "<b>{code}</b>"
TAG_LINE_TEMPLATE = "🏷️ {label}"
META_LINE_TEMPLATE = "💾 {size} · 🕒 {published}"
META_SIZE_ONLY_TEMPLATE = "💾 {size}"
META_TIME_ONLY_TEMPLATE = "🕒 {published}"
DESCRIPTION_PREFIX = "📝 "
#: A description made mostly of links, or carrying seller promotion, is not a plot.
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_PROMO_MARKERS = (
    "请关注",
    "請關注",
    "官方频道",
    "官方頻道",
    "不定期更新",
    "ランキング",
    "高画質版",
    "高畫質版",
    "特価",
    "特价",
    "pt→",
    "ご登録",
    "フォロー",
)
#: Below this much non-link text there is nothing to show.
MIN_DESCRIPTION_CHARS = 20
LINK_TEMPLATE = '<a href="{url}">nyaa</a>'
TIME_FORMAT = "%Y-%m-%d %H:%M"

CENSORSHIP_LABELS: dict[Censorship, str] = {
    Censorship.CENSORED: "有码",
    Censorship.UNCENSORED: "无码",
    Censorship.THIN: "薄码",
    Censorship.UNKNOWN: "未知",
}

CALLBACK_DOWNLOAD_PREFIX = "dl"
CALLBACK_BLOCK_PREFIX = "blk"
CALLBACK_DATA_LIMIT = 64


@dataclass(frozen=True, slots=True)
class CardSource:
    """Everything the card needs, independent of the database models."""

    content_id: str
    canonical_key: str
    display_code: str
    title: str | None
    description_ja: str | None
    description_zh: str | None
    censorship: Censorship
    nyaa_url: str
    official_url: str | None
    #: ``releases.raw_payload_json["size"]`` as stored by the source, e.g. ``3.72 GiB``.
    size_text: str | None = None
    #: ``releases.published_at``; rendered in :attr:`timezone`.
    published_at: datetime | None = None
    timezone: str = "Asia/Shanghai"


@dataclass(frozen=True, slots=True)
class CardMessages:
    caption: str
    extra_texts: tuple[str, ...]

    @property
    def has_overflow(self) -> bool:
        return bool(self.extra_texts)


def censorship_label(censorship: Censorship) -> str:
    return CENSORSHIP_LABELS.get(censorship, CENSORSHIP_LABELS[Censorship.UNKNOWN])


def download_callback(content_id: str) -> str:
    data = f"{CALLBACK_DOWNLOAD_PREFIX}:{content_id}"
    _ensure_short(data)
    return data


def block_callback(content_id: str) -> str:
    data = f"{CALLBACK_BLOCK_PREFIX}:{content_id}"
    _ensure_short(data)
    return data


def parse_callback(data: str) -> tuple[str, str] | None:
    if ":" not in data:
        return None
    action, _, content_id = data.partition(":")
    if action not in {CALLBACK_DOWNLOAD_PREFIX, CALLBACK_BLOCK_PREFIX} or not content_id:
        return None
    return action, content_id


def _ensure_short(data: str) -> None:
    if len(data.encode("utf-8")) > CALLBACK_DATA_LIMIT:
        raise ValueError(f"callback_data exceeds {CALLBACK_DATA_LIMIT} bytes: {data!r}")


def escape_html(text: str) -> str:
    """Escape text for ``parse_mode=HTML`` — ``<``/``>``/``&`` break the message."""

    return html.escape(text, quote=False)


def collapse_whitespace(text: str) -> str:
    """Fold runs of whitespace (newlines included) into single spaces."""

    return " ".join(text.split())


def truncate(text: str, limit: int) -> str:
    """Cut to ``limit`` characters, marking the cut with an ellipsis."""

    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


#: The code inside a title, e.g. ``FC2-PPV-5007844`` / ``FC2_5007844`` / ``FC2 5007844``.
#: Copied from the user's old project: case-insensitive, tolerant separators and
#: leading zeros, because the code is already printed bold on the first line.
_CODE_IN_TITLE_TEMPLATE = r"(?i)fc2[\s._-]*(?:ppv)?[\s._-]*0*{number}"
#: Torrent titles are often prefixed with ``+++``.
_LEADING_PLUS_RE = re.compile(r"^\s*\+{2,}\s*")


def clean_card_title(card: CardSource, *, limit: int | None = None) -> str:
    """The title without the repeated FC2 code, falling back to the code itself."""

    text = collapse_whitespace(card.title or "")
    text = _LEADING_PLUS_RE.sub("", text)
    number = (card.canonical_key or "").split(":")[-1]
    if number.isdigit():
        text = re.sub(_CODE_IN_TITLE_TEMPLATE.format(number=number), "", text)
    text = " ".join(text.split()).strip(" -_.")
    if not text:
        return card.display_code
    return truncate(text, limit) if limit else text


def usable_description(text: str | None) -> str | None:
    """The description only if it reads like a plot, not links or an ad.

    Nyaa posts are often a pile of image URLs or a seller's promotion ("join my
    channel", "half price"); the old system never showed those, and neither do we.
    """

    value = clean_description(text)
    if value is None:
        return None
    lowered = value.lower()
    if any(marker in lowered for marker in _PROMO_MARKERS):
        return None
    without_links = _URL_RE.sub("", value)
    link_chars = len(value) - len(without_links)
    if link_chars and link_chars / max(len(value), 1) > 0.3:
        return None
    if len(" ".join(without_links.split())) < MIN_DESCRIPTION_CHARS:
        return None
    return value


def clean_description(text: str | None, *, limit: int | None = None) -> str | None:
    if not text:
        return None
    value = collapse_whitespace(text)
    if not value:
        return None
    return truncate(value, limit) if limit else value


def format_published_at(value: datetime | None, timezone: str = "Asia/Shanghai") -> str | None:
    """Render a release timestamp as ``YYYY-MM-DD HH:MM`` in the configured zone.

    SQLite hands back naive datetimes; they were written by :func:`app.db.models.utcnow`
    so a naive value is UTC, not local time.
    """

    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        zone = ZoneInfo(timezone)
    except Exception:  # pragma: no cover - config validates the zone already
        zone = ZoneInfo("Asia/Shanghai")
    return value.astimezone(zone).strftime(TIME_FORMAT)


def meta_line(card: CardSource) -> str:
    """``💾 size · 🕒 time``; a missing size shows ``-``, as the old system did."""

    size = collapse_whitespace(card.size_text or "") or "-"
    published = format_published_at(card.published_at, card.timezone)
    if published:
        return META_LINE_TEMPLATE.format(size=escape_html(size), published=published)
    return META_SIZE_ONLY_TEMPLATE.format(size=escape_html(size))


def identity_block(card: CardSource, *, truncated: bool = False) -> str:
    """``<b>code</b>``, the tag line, then title + meta — the part that always stays."""

    lines = [CODE_LINE_TEMPLATE.format(code=escape_html(card.display_code))]
    lines.append(TAG_LINE_TEMPLATE.format(label=censorship_label(card.censorship)))
    limit = CARD_TITLE_MAX if truncated else None
    title = escape_html(clean_card_title(card, limit=limit))
    return f"{chr(10).join(lines)}\n\n{title}\n{meta_line(card)}"


def link_line(card: CardSource) -> str | None:
    if not card.nyaa_url:
        return None
    return LINK_TEMPLATE.format(url=html.escape(card.nyaa_url, quote=True))


def compact_caption(card: CardSource) -> str:
    """The caption used when the full card does not fit.

    Keeps the identifying block plus a **truncated preview** of the description and
    translation (200/250 characters, ellipsis visible) and the link. The complete
    body is sent as follow-up messages by :func:`description_messages`, so the
    preview never costs a single character of content.
    """

    blocks = [identity_block(card, truncated=True)]
    description = clean_description(card_text(card), limit=CARD_DESCRIPTION_MAX)
    if description:
        blocks.append(f"{DESCRIPTION_PREFIX}{escape_html(description)}")

    link = link_line(card)
    caption = "\n\n".join(blocks)
    if link:
        caption = f"{caption}\n{link}"
    if len(caption) <= CAPTION_LIMIT - CAPTION_SAFETY:
        return caption

    # Still too long (a pathological link or size string): drop the previews. The
    # full text follows as messages anyway, so nothing is lost.
    caption = identity_block(card, truncated=True)
    return f"{caption}\n{link}" if link else caption


def card_text(card: CardSource) -> str | None:
    """What the ``📝`` line shows: the Chinese translation, else the Japanese text.

    The old system printed one block, never both, and it never showed the Nyaa
    seller's post — the publisher only puts the official FC2 description here.
    """

    return usable_description(card.description_zh) or usable_description(card.description_ja)


def description_messages(card: CardSource) -> tuple[str, ...]:
    """The **complete** body as follow-up messages when the card does not fit.

    Nothing is truncated here: spec 9.1 forbids losing body text, so the caption's
    preview is only a preview.
    """

    description = clean_description(card_text(card))
    if not description:
        return ()
    return tuple(split_text(f"{DESCRIPTION_PREFIX}{escape_html(description)}"))


def build_card_messages(card: CardSource) -> CardMessages:
    """Build the caption and any follow-up text messages (spec 9.1).

    Layout (legacy template, HTML parse mode)::

        <b>FC2-PPV-1234567</b>
        🏷️ 无码

        标题（≤200 字）
        💾 3.72 GiB · 🕒 2025-11-15 03:01

        📝 简介（≤250 字）
        🇨🇳 中文翻译（≤250 字）
        <a href="...">nyaa</a>

    Policy (spec 9.1 + the user's compact layout):

    * everything fits → the caption is the **complete** card, nothing truncated;
    * it does not fit → the caption keeps the identifying block (title ≤200,
      description/translation preview ≤250, ellipsis visible) plus the link, and the
      **complete** description and translation follow as text messages. No body
      text is lost: the preview only ever duplicates what is sent in full.
    """

    description = clean_description(card_text(card))

    blocks = [identity_block(card)]
    if description:
        blocks.append(f"{DESCRIPTION_PREFIX}{escape_html(description)}")

    link = link_line(card)
    body = "\n\n".join(blocks)
    if link:
        body = f"{body}\n{link}"

    if len(body) <= CAPTION_LIMIT - CAPTION_SAFETY:
        return CardMessages(caption=body, extra_texts=())

    # Caption keeps the identifying block plus the link; the text follows separately.
    return CardMessages(caption=compact_caption(card), extra_texts=description_messages(card))


def split_text(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    """Split without losing characters, preferring paragraph/line boundaries."""

    remaining = text.strip()
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        window = remaining[:limit]
        cut = window.rfind("\n\n")
        if cut < limit // 2:
            cut = window.rfind("\n")
        if cut < limit // 2:
            cut = window.rfind(" ")
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    return [chunk for chunk in chunks if chunk]
