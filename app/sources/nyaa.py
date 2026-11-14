"""Nyaa Sukebei source adapter — spec 6 / 17.1.

Site-wide FC2 monitoring: RSS first, HTML search page as the catch-up path when
one round overflows the RSS window. No uploader filter is ever added (spec 6.1).

Design notes
------------
* Parsers are pure functions over bytes/HTML with the selectors kept in one place
  (spec 7.4): a site change is a one-line fix plus a new fixture, never a rewrite.
* ``discover()`` returns candidates only. It never calls the gate, the database or
  Telegram (spec 4). The poller records the cursor and asks the registrar, which
  evaluates the gate **before** writing a release (M1 contract).
* The overlap re-check works on **item id sets**, not on ``pubDate > cursor``:
  the whole current page is re-evaluated every round and the database decides
  what is new. The cursor keeps the last ``overlap_item_ids`` ids for pagination
  decisions and diagnostics (spec 6.2).
* RSS has no magnet link, only ``nyaa:infoHash``; magnets are built from the hash
  plus the configured trackers. The search page does carry real magnet links and
  they win when present.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

from defusedxml import ElementTree as DefusedElementTree
from selectolax.lexbor import LexborHTMLParser

from app.config import Settings
from app.logging import get_logger
from app.network.clients import HttpClient, HttpError
from app.schemas.release import NormalizedContent, ReleaseCandidate
from app.services.normalize import Fc2CodeRule

_log = get_logger("source.nyaa")

NYAA_SOURCE_NAME = "nyaa"


class NyaaParseError(RuntimeError):
    """The page did not look like the structure this adapter was written for."""


@dataclass(frozen=True, slots=True)
class NyaaItem:
    item_id: str
    title: str
    link: str | None = None
    pub_date: datetime | None = None
    info_hash: str | None = None
    magnet: str | None = None
    size_text: str | None = None
    category: str | None = None
    seeders: int | None = None
    uploader: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_http_date(value: str | None) -> datetime | None:
    """RFC 2822 (RSS ``pubDate``) or ISO-8601 timestamps."""

    if not value:
        return None
    candidate = value.strip()
    try:
        parsed = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        parsed = None
    if parsed is not None:
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    try:
        fallback = datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return fallback if fallback.tzinfo else fallback.replace(tzinfo=UTC)


def split_uploader(title: str) -> tuple[str | None, str]:
    """Nyaa titles are ``[Uploader] Title``; return both parts."""

    text = title.strip()
    if text.startswith("["):
        end = text.find("]")
        if 1 < end <= 64:
            uploader = text[1:end].strip()
            remainder = text[end + 1 :].strip()
            if uploader:
                return uploader, remainder or text
    return None, text


def item_id_from_url(link: str | None) -> str | None:
    """``https://sukebei.nyaa.si/view/1234567`` -> ``1234567``."""

    if not link:
        return None
    path = urlsplit(link).path.rstrip("/")
    candidate = path.rsplit("/", 1)[-1]
    return candidate or None


def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


class NyaaRssParser:
    """Parser for ``/?page=rss`` (spec 6.2 RSS-first path)."""

    ITEM_TAG = "item"

    def parse(self, payload: bytes) -> list[NyaaItem]:
        try:
            root = DefusedElementTree.fromstring(payload)
        except Exception as exc:
            raise NyaaParseError(f"invalid RSS payload: {type(exc).__name__}") from exc

        channel = None
        for child in root:
            if _localname(child.tag) == "channel":
                channel = child
                break
        if channel is None:
            raise NyaaParseError("RSS payload has no <channel>")

        items: list[NyaaItem] = []
        for element in channel:
            if _localname(element.tag) != self.ITEM_TAG:
                continue
            parsed = self._parse_item(element)
            if parsed is not None:
                items.append(parsed)
        return items

    def _parse_item(self, element: Any) -> NyaaItem | None:
        fields: dict[str, str] = {}
        for child in element:
            name = _localname(child.tag)
            text = (child.text or "").strip()
            if text:
                fields[name] = text

        title = fields.get("title", "")
        link = fields.get("link") or fields.get("guid")
        item_id = item_id_from_url(link) or fields.get("guid") or ""
        if not item_id:
            return None

        info_hash = (fields.get("infohash") or "").lower() or None
        seeders: int | None = None
        if fields.get("seeders", "").isdigit():
            seeders = int(fields["seeders"])

        uploader, clean_title = split_uploader(title)
        return NyaaItem(
            item_id=item_id,
            title=clean_title,
            link=link,
            pub_date=parse_http_date(fields.get("pubdate")),
            info_hash=info_hash,
            size_text=fields.get("size"),
            category=fields.get("category"),
            seeders=seeders,
            uploader=uploader,
            raw={
                "title": title,
                "guid": fields.get("guid"),
                "categoryId": fields.get("categoryid"),
                "trusted": fields.get("trusted"),
                "remake": fields.get("remake"),
                "description": fields.get("description", "")[:2000],
            },
        )


class NyaaSearchParser:
    """Parser for the HTML search page (spec 6.2 catch-up path).

    Selectors are deliberately centralised: a markup change is fixed here and
    covered by a new fixture.
    """

    TABLE = "table.torrent-list"
    ROW = "tbody tr"
    TITLE_LINK = "a[href*='/view/']"
    MAGNET_LINK = "a[href^='magnet:']"
    SIZE_CELL = "td.td-size"
    DATE_CELL = "td[data-timestamp]"
    SEEDERS_CELL = "td.td-green"
    CATEGORY_LINK = "td a[href*='c=']"

    def parse(self, html: str, base_url: str) -> list[NyaaItem]:
        tree = LexborHTMLParser(html)
        table = tree.css_first(self.TABLE)
        if table is None:
            raise NyaaParseError(f"search page has no {self.TABLE}")

        items: list[NyaaItem] = []
        for row in table.css(self.ROW):
            title_link = row.css_first(self.TITLE_LINK)
            if title_link is None:
                continue
            href = title_link.attributes.get("href") or ""
            link = urljoin(base_url, href)
            item_id = item_id_from_url(link)
            if not item_id:
                continue

            magnet_node = row.css_first(self.MAGNET_LINK)
            magnet = magnet_node.attributes.get("href") if magnet_node else None
            info_hash = _hash_from_magnet(magnet)

            size_node = row.css_first(self.SIZE_CELL)
            date_node = row.css_first(self.DATE_CELL)
            seeders_node = row.css_first(self.SEEDERS_CELL)
            category_node = row.css_first(self.CATEGORY_LINK)

            pub_date = None
            if date_node is not None:
                stamp = date_node.attributes.get("data-timestamp")
                if stamp and stamp.isdigit():
                    pub_date = datetime.fromtimestamp(int(stamp), tz=UTC)

            seeders = None
            if seeders_node is not None and seeders_node.text(strip=True).isdigit():
                seeders = int(seeders_node.text(strip=True))

            uploader, clean_title = split_uploader(title_link.text(strip=True))
            items.append(
                NyaaItem(
                    item_id=item_id,
                    title=clean_title,
                    link=link,
                    pub_date=pub_date,
                    info_hash=info_hash,
                    magnet=magnet,
                    size_text=size_node.text(strip=True) if size_node else None,
                    category=(category_node.attributes.get("title") if category_node else None),
                    seeders=seeders,
                    uploader=uploader,
                    raw={
                        "href": href,
                        "magnet_present": bool(magnet),
                        "row_class": row.attributes.get("class"),
                    },
                )
            )
        return items


def _hash_from_magnet(magnet: str | None) -> str | None:
    if not magnet or "urn:btih:" not in magnet:
        return None
    candidate = magnet.split("urn:btih:", 1)[1].split("&", 1)[0].strip().lower()
    if len(candidate) == 40 and all(char in "0123456789abcdef" for char in candidate):
        return candidate
    return None


def build_magnet(info_hash: str, title: str, trackers: list[str]) -> str:
    parts = [
        f"magnet:?xt=urn:btih:{info_hash.lower()}",
        f"dn={quote(title, safe='')}",
    ]
    parts.extend(f"tr={quote(tracker, safe='')}" for tracker in trackers)
    return "&".join(parts)


@dataclass(frozen=True, slots=True)
class NyaaCursor:
    """Persisted polling state (``source_cursors.cursor_json``)."""

    etag: str | None = None
    last_modified: str | None = None
    seen_item_ids: tuple[str, ...] = ()
    last_success_at: str | None = None
    last_pub_date: str | None = None
    last_item_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> NyaaCursor:
        if not data:
            return cls()
        raw_ids = data.get("seen_item_ids") or []
        seen = tuple(str(item) for item in raw_ids if item)[:2000]
        return cls(
            etag=_str_or_none(data.get("etag")),
            last_modified=_str_or_none(data.get("last_modified")),
            seen_item_ids=seen,
            last_success_at=_str_or_none(data.get("last_success_at")),
            last_pub_date=_str_or_none(data.get("last_pub_date")),
            last_item_id=_str_or_none(data.get("last_item_id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "etag": self.etag,
            "last_modified": self.last_modified,
            "seen_item_ids": list(self.seen_item_ids),
            "last_success_at": self.last_success_at,
            "last_pub_date": self.last_pub_date,
            "last_item_id": self.last_item_id,
        }

    def with_items(self, items: list[NyaaItem], *, limit: int) -> NyaaCursor:
        """Remember the newest ids for the overlap re-check."""

        fresh = [item.item_id for item in items]
        merged: list[str] = []
        for item_id in fresh + list(self.seen_item_ids):
            if item_id not in merged:
                merged.append(item_id)
        newest = max(
            (item.pub_date for item in items if item.pub_date is not None),
            default=None,
        )
        return replace(
            self,
            seen_item_ids=tuple(merged[:limit]),
            last_success_at=datetime.now(UTC).isoformat(),
            last_item_id=fresh[0] if fresh else self.last_item_id,
            last_pub_date=newest.isoformat() if newest else self.last_pub_date,
        )


def _str_or_none(value: Any) -> str | None:
    return str(value) if value else None


@dataclass(slots=True)
class DiscoveryStats:
    """Per-round counters for the spec 21 metrics."""

    pages_fetched: int = 0
    rss_items: int = 0
    not_modified: bool = False
    filtered: dict[str, str] = field(default_factory=dict)
    new_item_ids: list[str] = field(default_factory=list)


class NyaaSource:
    """Spec 17.1 adapter for ``sukebei.nyaa.si``."""

    name = NYAA_SOURCE_NAME

    def __init__(
        self,
        *,
        settings: Settings,
        client: HttpClient | None = None,
        rule: Fc2CodeRule | None = None,
    ) -> None:
        self.settings = settings
        self.config = settings.nyaa
        self.rule = rule or Fc2CodeRule(settings.fc2.code_min_digits, settings.fc2.code_max_digits)
        self.rss_parser = NyaaRssParser()
        self.search_parser = NyaaSearchParser()
        self.client = client or HttpClient(
            user_agent=self.config.user_agent,
            timeout_seconds=self.config.timeout_seconds,
            retries=self.config.retries,
            per_domain_concurrency=self.config.concurrency,
            max_retry_after_seconds=self.config.max_retry_after_seconds,
        )
        #: Set after every discover() call; the poller persists it.
        self.last_cursor: NyaaCursor | None = None
        self.last_stats = DiscoveryStats()

    async def aclose(self) -> None:
        await self.client.aclose()

    # -- discovery ----------------------------------------------------------

    async def discover(self, cursor: dict[str, Any] | None) -> list[ReleaseCandidate]:
        state = NyaaCursor.from_dict(cursor)
        first_run = not cursor
        self.last_stats = DiscoveryStats()
        stats = self.last_stats

        items, state = await self._fetch_rss(state, stats)
        if stats.not_modified:
            self.last_cursor = state
            return []

        if first_run:
            cutoff = datetime.now(UTC) - timedelta(hours=self.config.initial_lookback_hours)
            kept = [item for item in items if item.pub_date is None or item.pub_date >= cutoff]
            kept_ids = {item.item_id for item in kept}
            stats.filtered.update(
                {
                    item.item_id: "older_than_lookback"
                    for item in items
                    if item.item_id not in kept_ids
                }
            )
            items = kept

        # One RSS page is the window; if it is full, new items may have spilled
        # into the next pages (spec 6.2).
        if len(items) >= self.config.rss_page_size:
            items = await self._paginate(items, stats)

        unique: list[NyaaItem] = []
        seen: set[str] = set()
        for item in items:
            if item.item_id in seen:
                continue
            seen.add(item.item_id)
            unique.append(item)
        stats.rss_items = len(unique)
        stats.new_item_ids = [
            item.item_id for item in unique if item.item_id not in state.seen_item_ids
        ]

        candidates: list[ReleaseCandidate] = []
        for item in unique:
            candidate, reason = self._to_candidate(item)
            if candidate is None:
                stats.filtered[item.item_id] = reason or "unusable"
                continue
            candidates.append(candidate)

        self.last_cursor = state.with_items(unique, limit=self.config.overlap_item_ids)
        _log.info(
            "nyaa.discover",
            items=stats.rss_items,
            candidates=len(candidates),
            filtered=len(stats.filtered),
            new_items=len(stats.new_item_ids),
            pages=stats.pages_fetched,
            source=NYAA_SOURCE_NAME,
        )
        return candidates

    # -- HTTP ---------------------------------------------------------------

    async def _fetch_rss(
        self, state: NyaaCursor, stats: DiscoveryStats
    ) -> tuple[list[NyaaItem], NyaaCursor]:
        headers: dict[str, str] = {}
        if state.etag:
            headers["If-None-Match"] = state.etag
        if state.last_modified:
            headers["If-Modified-Since"] = state.last_modified

        url = self.config.base_url.rstrip("/") + self.config.rss_path
        response = await self.client.get(url, headers=headers)
        stats.pages_fetched += 1
        if response.not_modified:
            stats.not_modified = True
            _log.info("nyaa.not_modified", source=NYAA_SOURCE_NAME)
            return [], state

        # Keep the validators for the next conditional request (spec 6.2).
        updated = replace(
            state,
            etag=response.header("ETag") or state.etag,
            last_modified=response.header("Last-Modified") or state.last_modified,
        )
        return self.rss_parser.parse(response.content), updated

    async def _paginate(self, items: list[NyaaItem], stats: DiscoveryStats) -> list[NyaaItem]:
        collected = list(items)
        known = {item.item_id for item in collected}
        for page in range(2, self.config.search_max_pages + 1):
            try:
                extra = await self._fetch_search_page(page)
            except (HttpError, NyaaParseError) as exc:
                # Catch-up is best effort: the RSS round must still succeed.
                _log.warning("nyaa.pagination_failed", page=page, error_code=type(exc).__name__)
                break
            fresh = [item for item in extra if item.item_id not in known]
            collected.extend(fresh)
            known.update(item.item_id for item in fresh)
            if len(extra) < self.config.rss_page_size:
                break
        return collected

    async def _fetch_search_page(self, page: int) -> list[NyaaItem]:
        path = self.config.search_path.format(page=page)
        url = self.config.base_url.rstrip("/") + path
        self.last_stats.pages_fetched += 1
        response = await self.client.get(url)
        return self.search_parser.parse(response.text, self.config.base_url)

    # -- mapping ------------------------------------------------------------

    def _to_candidate(self, item: NyaaItem) -> tuple[ReleaseCandidate | None, str | None]:
        normalized: NormalizedContent | None = self.rule.normalize(item.title)
        if normalized is None:
            return None, "not_fc2"

        info_hash = item.info_hash or _hash_from_magnet(item.magnet)
        magnet = item.magnet
        if not magnet and info_hash:
            magnet = build_magnet(info_hash, item.title, list(self.config.trackers))
        if not magnet:
            return None, "no_magnet"

        return (
            ReleaseCandidate(
                source=NYAA_SOURCE_NAME,
                source_item_id=item.item_id,
                source_url=item.link,
                title=item.title,
                published_at=item.pub_date,
                magnet=magnet,
                info_hash=info_hash,
                description=None,
                assets=(),
                content_hint=normalized,
                censorship_hint=None,
                raw_payload={
                    **item.raw,
                    "uploader": item.uploader,
                    "size": item.size_text,
                    "category": item.category,
                    "seeders": item.seeders,
                    "info_hash": info_hash,
                },
            ),
            None,
        )
