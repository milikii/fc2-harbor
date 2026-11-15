"""Metadata provider schemas (spec 7.2).

The models exist from M1 so that the database columns, the gate and the future
provider adapters agree on one shape. Provider *implementations* arrive in M3.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.db.enums import Censorship


class AssetCandidate(BaseModel):
    """One image proposed by a provider, before it is downloaded."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    url: str
    provider: str
    role: str = "preview"
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)
    referer: str | None = None

    @field_validator("url")
    @classmethod
    def _http_only(cls, value: str) -> str:
        lowered = value.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError(f"asset url must be http(s), got {value!r}")
        return value


class MetadataResult(BaseModel):
    """Spec 7.2 — unified provider result, merged field by field by priority."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    provider: str
    content_key: str
    title_ja: str | None = None
    description_ja: str | None = None
    #: How good ``description_ja`` is. A ``summary`` is a teaser (site-truncated
    #: meta description or a search-result snippet); ``full`` is body text. The
    #: coordinator lets a ``full`` description replace a ``summary`` even from a
    #: lower-priority provider (see docs/deviations.md §35).
    description_quality: Literal["full", "summary"] | None = None
    official_url: str | None = None
    censorship: Censorship | None = None
    release_date: datetime | None = None
    cover: AssetCandidate | None = None
    previews: list[AssetCandidate] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    def has_core_fields(self) -> bool:
        return bool(self.title_ja and self.description_ja)


class ProviderContext(BaseModel):
    """Everything a provider adapter is allowed to know (spec 17.2)."""

    model_config = ConfigDict(extra="forbid", frozen=True, arbitrary_types_allowed=True)

    canonical_key: str
    display_code: str | None = None
    proxy_profile: str = "direct"
    timeout_seconds: float = 15.0
    max_previews: int = 9
    referer: str | None = None
    #: The release the content came from, used by the Nyaa fallback (spec 7.2).
    release_url: str | None = None
    release_item_id: str | None = None
