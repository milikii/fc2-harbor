"""Normalised release/content schemas shared by sources, gate and bots.

Spec references: section 6.3 (FC2 canonical keys), section 17.1
(``ReleaseCandidate``), section 10 (manual submission).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.db.enums import Censorship, ContentType


@dataclass(frozen=True, slots=True)
class NormalizedContent:
    """Result of normalising a title/name into a canonical content identity."""

    content_type: ContentType
    canonical_key: str
    display_code: str | None
    matched_text: str | None = None


@dataclass(frozen=True, slots=True)
class MagnetInfo:
    """A parsed ``magnet:`` URI."""

    raw: str
    info_hash: str
    display_name: str | None = None
    trackers: tuple[str, ...] = ()

    @property
    def dedupe_key(self) -> str:
        return f"infohash:{self.info_hash}"


@dataclass(frozen=True, slots=True)
class ReleaseCandidate:
    """Spec 17.1 — what a source adapter produces.

    The gate is always evaluated *before* a release row is written, so this is
    the only carrier of "not yet persisted" release data.
    """

    source: str
    source_item_id: str
    source_url: str | None
    title: str
    published_at: datetime | None = None
    magnet: str | None = None
    info_hash: str | None = None
    description: str | None = None
    assets: tuple[str, ...] = ()
    content_hint: NormalizedContent | None = None
    censorship_hint: Censorship | None = None
    raw_payload: dict[str, Any] = field(default_factory=dict)
