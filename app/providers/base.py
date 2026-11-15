"""Metadata provider plumbing — spec 7.4 / 17.2.

Shared here: HTTP fetch, error types, logging, latency, and a handful of pure
extraction helpers (meta tags, first matching selector, absolute URLs).

Deliberately *not* shared: parsing. Each provider has its own ``parse`` with its
own selectors, because a single generic parser across nine unrelated sites is the
maintenance trap spec 7.4 warns about. A provider whose page no longer matches its
selectors raises :class:`ProviderError` — it never returns a half-empty success.
"""

from __future__ import annotations

import asyncio
import html
import random
import re
import time
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser

from app.db.enums import Censorship
from app.db.models import Content
from app.logging import get_logger
from app.network.browser_headers import provider_headers
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
    #: The release title (e.g. the Nyaa torrent title) the lookup started from.
    #: Providers may use it as evidence for the censorship tag.
    release_title: str | None = None
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
    """Normalise provider text: decode HTML entities **once**, then fold whitespace.

    Sources hand out ``&quot;`` / ``&amp;`` / ``&#39;`` in meta tags and titles, and
    the card layer re-escapes everything for ``parse_mode=HTML``. Without this step
    a single quote became ``&amp;quot;`` on the card.

    Exactly one pass on purpose: a loop would turn a literal ``&amp;amp;`` into
    ``&`` and eat the user's text. ``&nbsp;`` decodes to U+00A0, which ``split()``
    treats as whitespace, so it is folded away with the rest.
    """

    if value is None:
        return None
    text = " ".join(html.unescape(value).split())
    return text or None


#: Explicit wording only (spec 7.3) — never inferred from a filename. FC2 and Nyaa
#: titles use the short forms ``【無】`` / ``【有】`` / ``【薄】`` far more often than
#: the long ones, which is why the tag used to be UNKNOWN almost always.
#: ``モザイク`` means mosaic censoring; when in doubt it is CENSORED, never THIN.
CENSORSHIP_TOKENS: tuple[tuple[str, Censorship], ...] = (
    ("無修正", Censorship.UNCENSORED),
    ("无修正", Censorship.UNCENSORED),
    ("無碼", Censorship.UNCENSORED),
    ("无码", Censorship.UNCENSORED),
    ("【無】", Censorship.UNCENSORED),
    ("【无】", Censorship.UNCENSORED),
    ("uncensored", Censorship.UNCENSORED),
    ("薄修正", Censorship.THIN),
    ("薄码", Censorship.THIN),
    ("薄碼", Censorship.THIN),
    ("【薄】", Censorship.THIN),
    ("有修正", Censorship.CENSORED),
    ("修正あり", Censorship.CENSORED),
    ("有碼", Censorship.CENSORED),
    ("有码", Censorship.CENSORED),
    ("【有】", Censorship.CENSORED),
    ("censored", Censorship.CENSORED),
    ("モザイク", Censorship.CENSORED),
)
#: ``【無・ごっくん＆中出し】`` is extremely common on FC2: the tag is a *prefix*
#: inside the bracket, not the whole bracket. Matching only ``【無】`` missed them.
_BRACKET_RE = re.compile(r"[【\[]([^】\]]{0,40})[】\]]")
_BRACKET_PREFIXES: tuple[tuple[str, Censorship], ...] = (
    ("無", Censorship.UNCENSORED),
    ("无", Censorship.UNCENSORED),
    ("有", Censorship.CENSORED),
    ("薄", Censorship.THIN),
)
#: ``【無料】`` / ``【無制限】`` / ``【無断】`` are not about censoring. Never guess.
_BRACKET_EXCLUSIONS: dict[Censorship, tuple[str, ...]] = {
    Censorship.UNCENSORED: (
        "料",
        "制限",
        "断",
        "償",
        "垢",
        "名",
        "敵",
        "条件",
        "許可",
        "料金",
        "添加",
        "香料",
        "修正しない",
    ),
    Censorship.CENSORED: ("料", "料金", "名", "限", "効", "益", "用", "罪", "機", "志"),
    Censorship.THIN: (),
}

#: When one text yields several tags, the more specific one wins.
_CENSORSHIP_PRECEDENCE = (
    Censorship.UNCENSORED,
    Censorship.THIN,
    Censorship.CENSORED,
)


def normalise_for_match(value: str) -> str:
    """NFKC (full-width → half-width) + lowercase, for token matching."""

    return unicodedata.normalize("NFKC", value).lower()


def infer_censorship(
    fields: Mapping[str, str | None],
) -> tuple[Censorship | None, dict[str, object]]:
    """Read the censorship tag from explicit wording in several fields.

    ``fields`` maps a source name (``title`` / ``description`` / ``release_title`` /
    ``page``) to its text. Returns the verdict plus the evidence that produced it, so
    a tag can always be explained afterwards.
    """

    matches: list[dict[str, str]] = []
    verdicts: set[Censorship] = set()
    for field_name, value in fields.items():
        if not value:
            continue
        normalised = normalise_for_match(value)
        for bracket_match in _BRACKET_RE.finditer(normalised):
            inner = bracket_match.group(1).strip()
            if not inner:
                continue
            for prefix, verdict in _BRACKET_PREFIXES:
                if not inner.startswith(prefix):
                    continue
                if inner[1:].startswith(_BRACKET_EXCLUSIONS[verdict]):
                    break  # 【無料】 and friends: not a censoring tag
                matches.append(
                    {
                        "field": field_name,
                        "token": f"【{inner}】",
                        "verdict": verdict.value,
                        "excerpt": value[
                            max(0, bracket_match.start() - 12) : bracket_match.end() + 12
                        ].strip(),
                    }
                )
                verdicts.add(verdict)
                break
        for token, verdict in CENSORSHIP_TOKENS:
            index = normalised.find(normalise_for_match(token))
            if index < 0:
                continue
            start = max(0, index - 12)
            matches.append(
                {
                    "field": field_name,
                    "token": token,
                    "verdict": verdict.value,
                    "excerpt": value[start : index + len(token) + 12].strip(),
                }
            )
            verdicts.add(verdict)
            break

    deduped: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in matches:
        key = (str(item["field"]), str(item["token"]))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    matches = deduped

    evidence: dict[str, object] = {"matches": matches, "conflict": False}
    if not verdicts:
        return None, evidence
    if Censorship.UNCENSORED in verdicts and (
        Censorship.CENSORED in verdicts or Censorship.THIN in verdicts
    ):
        # Both "uncensored" and a censored wording: refuse to pick a winner.
        evidence["conflict"] = True
        evidence["verdicts"] = sorted(item.value for item in verdicts)
        return Censorship.UNKNOWN, evidence
    for candidate in _CENSORSHIP_PRECEDENCE:
        if candidate in verdicts:
            return candidate, evidence
    return None, evidence  # pragma: no cover - every verdict is in the list


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
        headers: Mapping[str, str] | None = None,
        warmup_url: str | None = None,
        min_interval_seconds: float = 0.0,
        jitter_seconds: float = 0.0,
    ) -> None:
        self.client = client
        self.proxy_url = proxy_url
        self.timeout_seconds = timeout_seconds
        self.proxy_region = proxy_region
        self.headers = dict(headers or {})
        self.warmup_url = warmup_url
        self.min_interval_seconds = min_interval_seconds
        self.jitter_seconds = jitter_seconds
        self._warmed = False
        self._last_request = 0.0

    # -- URL construction (per provider) ------------------------------------

    def build_url(self, content: Content) -> str:
        raise NotImplementedError

    # -- fetch --------------------------------------------------------------

    async def _throttle(self) -> None:
        """Stay under a per-site request rate, with a little jitter."""

        if self.min_interval_seconds <= 0 and self.jitter_seconds <= 0:
            return
        if self._last_request:
            wait = (
                self._last_request
                + self.min_interval_seconds
                + random.uniform(0, self.jitter_seconds)  # noqa: S311 - pacing, not crypto
                - time.monotonic()
            )
            if wait > 0:
                await asyncio.sleep(wait)
        self._last_request = time.monotonic()

    async def _warm_up(self) -> str | None:
        """Visit the site once so cookies exist and the referer is same-origin.

        Failure is fine: the real request still goes out with browser headers.
        """

        if self._warmed or not self.warmup_url:
            return self.referer
        self._warmed = True
        try:
            await self.client.get(
                self.warmup_url,
                headers=provider_headers(self.host, overrides=self.headers),
                proxy_url=self.proxy_url,
                proxy_label=self.proxy_key or self.id,
                proxy_region=self.proxy_region,
            )
        except HttpError as exc:
            _log.info(
                "provider.warmup_failed",
                provider=self.id,
                url=self.warmup_url,
                status=exc.status,
            )
            return self.referer
        return self.warmup_url

    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        url = self.build_url(content)
        await self._throttle()
        referer = await self._warm_up() or self.referer
        headers = provider_headers(self.host, referer=referer, overrides=self.headers)
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

        page = ProviderPage(
            url=url,
            html=response.text,
            status=response.status,
            release_title=ctx.release_title,
        )
        extra = await self.fetch_extra(page, content, ctx)
        if extra:
            page = ProviderPage(
                url=url,
                html=response.text,
                status=response.status,
                release_title=ctx.release_title,
                extra=extra,
            )
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
