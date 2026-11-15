"""Metadata provider plumbing — spec 7.4 / 17.2.

Shared here: HTTP fetch, error types, logging, latency, and a handful of pure
extraction helpers (meta tags, first matching selector, absolute URLs).

Deliberately *not* shared: parsing. Each provider has its own ``parse`` with its
own selectors, because a single generic parser across nine unrelated sites is the
maintenance trap spec 7.4 warns about. A provider whose page no longer matches its
selectors raises :class:`ProviderError` — it never returns a half-empty success.
"""

from __future__ import annotations

import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from app.db.models import Content
from app.logging import get_logger
from app.network.clients import HttpClient, HttpError
from app.schemas.metadata import AssetCandidate, MetadataResult, ProviderContext

_log = get_logger("provider")

#: selectolax trees and nodes expose the same query API; the package ships no
#: usable typing, so helpers accept either shape.
type Queryable = Any


class ProviderError(RuntimeError):
    """A provider could not produce a usable result."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status


@dataclass(frozen=True, slots=True)
class ProviderPage:
    url: str
    html: str
    status: int
    #: Extra documents a provider had to fetch (e.g. FC2's description widget),
    #: keyed by name. Empty for the providers that only need one page.
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def tree(self) -> LexborHTMLParser:
        return LexborHTMLParser(self.html)


def content_number(content: Content) -> str:
    """``fc2_ppv:1234567`` → ``1234567``."""

    key = content.canonical_key or ""
    return key.split(":", 1)[1] if ":" in key else key


def content_code(content: Content) -> str:
    """Display code, falling back to the canonical key."""

    return content.display_code or content.canonical_key


#: A site title may write the number as ``FC2-PPV-1234567``, ``FC2-1234567``,
#: ``FC2PPV1234567`` or ``FC2 PPV 1234567``. Matching the full display code as a
#: plain substring therefore misses most real titles, so every provider compares
#: the **number** with tolerant separators instead (spec 6.3).
_CODE_PATTERN_CACHE: dict[str, re.Pattern[str]] = {}


def code_matches(title: str | None, content: Content) -> bool:
    """True when ``title`` refers to the same FC2 number as ``content``."""

    if not title:
        return False
    number = content_number(content)
    if not number.isdigit():
        return False
    pattern = _CODE_PATTERN_CACHE.get(number)
    if pattern is None:
        pattern = re.compile(
            rf"(?<![0-9A-Za-z])FC2[\s\-_.]*(?:PPV)?[\s\-_.]*{re.escape(number)}(?![0-9])",
            re.IGNORECASE,
        )
        _CODE_PATTERN_CACHE[number] = pattern
    return pattern.search(title) is not None


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    return text or None


def meta_content(tree: Queryable, *names: str) -> str | None:
    """Read ``<meta property|name=… content=…>`` (og:title, description, …)."""

    for name in names:
        for selector in (f'meta[property="{name}"]', f'meta[name="{name}"]'):
            node = tree.css_first(selector)
            if node is not None:
                text = clean_text(node.attributes.get("content"))
                if text:
                    return text
    return None


def first_text(tree: Queryable, selectors: Iterable[str]) -> str | None:
    for selector in selectors:
        node = tree.css_first(selector)
        if node is not None:
            text = clean_text(node.text(strip=True))
            if text:
                return text
    return None


def first_attr(tree: LexborHTMLParser, selectors: Iterable[str], attribute: str) -> str | None:
    for selector in selectors:
        node = tree.css_first(selector)
        if node is None:
            continue
        value = clean_text(node.attributes.get(attribute))
        if value:
            return value
    return None


def image_src(node: Any | None, base: str) -> str | None:
    """Image URL from a node, preferring lazy-load attributes over placeholders.

    Real listings often ship a 1x1 placeholder in ``src`` and the real URL in
    ``data-src`` (freejavbt) or only ``src`` (javdb). Prefer the lazy attribute.
    """

    if node is None:
        return None
    for attribute in ("data-src", "data-original", "src"):
        value = clean_text(node.attributes.get(attribute))
        if value and not value.startswith("data:"):
            return absolute_url(value, base)
    return None


def absolute_url(value: str | None, base: str) -> str | None:
    if not value:
        return None
    return urljoin(base, value)


def asset(
    url: str | None,
    provider: str,
    *,
    role: str = "preview",
    referer: str | None = None,
) -> AssetCandidate | None:
    if not url:
        return None
    return AssetCandidate(url=url, provider=provider, role=role, referer=referer)


def dedupe_urls(urls: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if not url or url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered


class BaseProvider:
    """One adapter per site. Subclasses define ``id``, ``host`` and ``parse``."""

    id: ClassVar[str] = ""
    host: ClassVar[str] = ""
    #: Config key under ``provider.sources``.
    proxy_key: ClassVar[str] = ""
    #: Extra request headers, usually a referer for hotlink protection.
    referer: ClassVar[str | None] = None

    def __init__(
        self,
        *,
        client: HttpClient,
        proxy_url: str | None = None,
        timeout_seconds: float = 15.0,
        proxy_region: str | None = None,
    ) -> None:
        self.client = client
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds
        self.proxy_region = proxy_region

    # -- URL construction (per provider) ------------------------------------

    def build_url(self, content: Content) -> str:
        raise NotImplementedError

    # -- fetch --------------------------------------------------------------

    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        url = self.build_url(content)
        headers = {"Referer": self.referer} if self.referer else {}
        started = time.monotonic()
        try:
            response = await self.client.get(
                url,
                headers=headers,
                proxy_url=self.proxy_url,
                proxy_label=self.proxy_key or self.id,
                proxy_region=self.proxy_region,
            )
        except HttpError as exc:
            # A 404 on a search-by-code page is a normal miss, not a broken site.
            code = "not_found" if exc.status == 404 else "http_error"
            _log.info(
                "provider.fetch_failed",
                provider=self.id,
                domain=self.host,
                status=exc.status,
                latency_ms=int((time.monotonic() - started) * 1000),
                error_code=code,
            )
            raise ProviderError(code, f"{self.id}: {exc}", http_status=exc.status) from exc
        if not 200 <= response.status < 300:
            raise ProviderError(
                "http_error", f"{self.id}: status {response.status}", http_status=response.status
            )

        page = ProviderPage(url=url, html=response.text, status=response.status)
        extra = await self.fetch_extra(page, content, ctx)
        if extra:
            page = ProviderPage(url=url, html=response.text, status=response.status, extra=extra)
        result = self.parse(page, content)
        latency_ms = int((time.monotonic() - started) * 1000)
        _log.info(
            "provider.fetch",
            provider=self.id,
            domain=self.host,
            status=response.status,
            latency_ms=latency_ms,
            found=result is not None,
            asset_count=(len(result.previews) + (1 if result.cover else 0)) if result else 0,
        )
        if result is None:
            raise ProviderError("not_found", f"{self.id}: no matching entry", http_status=200)
        return result

    async def fetch_extra(
        self, page: ProviderPage, content: Content, ctx: ProviderContext
    ) -> dict[str, str]:
        """Fetch further documents the parser needs (default: none).

        Failures here must be non-fatal: a missing secondary document degrades the
        result, it never turns a found entry into a miss.
        """

        return {}

    # -- parsing (per provider) --------------------------------------------

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        raise NotImplementedError

    # -- helpers for subclasses --------------------------------------------

    def _result(
        self,
        content: Content,
        *,
        title: str | None = None,
        description: str | None = None,
        description_quality: str | None = None,
        official_url: str | None = None,
        censorship: object = None,
        release_date: object = None,
        cover: str | None = None,
        previews: Iterable[str | None] = (),
        raw: dict[str, object] | None = None,
    ) -> MetadataResult:
        cover_asset = asset(cover, self.id, role="cover", referer=self.referer)
        preview_assets = [
            item
            for url in dedupe_urls(previews)
            if (item := asset(url, self.id, role="preview", referer=self.referer)) is not None
        ]
        return MetadataResult(
            provider=self.id,
            content_key=content.canonical_key,
            title_ja=clean_text(title),
            description_ja=clean_text(description),
            description_quality=description_quality,  # type: ignore[arg-type]
            official_url=official_url,
            censorship=censorship,  # type: ignore[arg-type]
            release_date=release_date,  # type: ignore[arg-type]
            cover=cover_asset,
            previews=preview_assets,
            raw=raw or {},
        )
