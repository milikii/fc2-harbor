"""Telegram card construction — spec 9.1 / 9.2.

Two hard rules from the spec:

* the first message is a photo whose caption carries tag, title, FC2 code and the
  Nyaa link; if the description does not fit in a caption it is sent as following
  text messages — the body is **never** silently truncated;
* ``callback_data`` stays short (``dl:<uuid>`` / ``blk:<uuid>``), never a magnet or
  a URL, and always below Telegram's 64-byte limit.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.db.enums import Censorship

CAPTION_LIMIT = 1024
TEXT_LIMIT = 4096
#: Room left for the ellipsis/separators when packing caption text.
CAPTION_SAFETY = 64

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


def build_card_messages(card: CardSource) -> CardMessages:
    """Build the caption and any follow-up text messages (spec 9.1)."""

    header_lines = [f"{censorship_label(card.censorship)}｜{card.title or card.display_code}"]
    header_lines.append(card.display_code)
    header_lines.append(card.nyaa_url)
    header = "\n".join(header_lines)

    body_parts: list[str] = []
    if card.description_ja:
        body_parts.append(card.description_ja.strip())
    if card.description_zh:
        body_parts.append(f"中文：{card.description_zh.strip()}")
    body = "\n\n".join(part for part in body_parts if part)

    if not body:
        return CardMessages(caption=header, extra_texts=())

    combined = f"{header}\n\n{body}"
    if len(combined) <= CAPTION_LIMIT - CAPTION_SAFETY:
        return CardMessages(caption=combined, extra_texts=())

    # Caption keeps the identifying block only; the full text follows separately.
    return CardMessages(caption=header, extra_texts=tuple(split_text(body)))


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
