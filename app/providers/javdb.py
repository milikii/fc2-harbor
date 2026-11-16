"""JavDB metadata.

Spec 7.1 priority 3. Real results are ``.movie-list .item > a.box`` with a
``.cover img`` and a ``.video-title`` that starts with the code in a ``<strong>``
(``FC2-5000075`` short form). JavDB indexes FC2 releases, so it is one of the more
useful sources; a miss is a normal outcome, not a failure.

Two measured behaviours shape this adapter:

* anonymous requests are **rate limited**, not blocked: ~7–8 s between requests
  answers 200 / 36 KB, back-to-back requests answer 403 with an empty body. The
  provider therefore paces itself and reports throttling as ``rate_limited``, never
  as ``structure_changed`` (which used to hide 38 real throttles as broken markup);
* a search may return a *near* number (``4906053`` for ``4906073``). Only an exact
  number match is accepted — no data beats wrong data.
"""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from urllib.parse import quote

from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    content_code,
    content_number,
    first_text,
    image_src,
    infer_censorship,
)
from app.schemas.metadata import MetadataResult

#: Anonymous javdb tolerates roughly one request every 7–8 seconds.
JAVDB_MIN_INTERVAL_SECONDS = 7.0
JAVDB_JITTER_SECONDS = 2.0
#: A 403 is worth one retry after this pause; a second one means "come back later".
JAVDB_RATE_LIMIT_PAUSE_SECONDS = 15.0
RATE_LIMIT_STATUSES = frozenset({403, 429})
#: A real search page is tens of kilobytes; anything tiny is a throttle stub.
MIN_PAGE_BYTES = 500
#: Markers of the login wall. Measured 2026-11-16: an anonymous search returns
#: ``<title>登入 | JavDB</title>`` with no results at all, even when paced.
LOGIN_MARKERS = ("登入", "登録", "請先登入", "请先登录")
#: ``FC2-4906073`` / ``FC2-PPV-4906073`` at the start of a card title.
_TITLE_NUMBER_RE = re.compile(r"FC2[\s._-]*(?:PPV)?[\s._-]*(\d{5,8})", re.IGNORECASE)


def _parse_meta_date(value: str | None) -> datetime | None:
    """``2026-11-14`` → datetime (UTC); anything else is ignored."""

    if not value:
        return None
    match = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", value.strip())
    if match is None:
        return None
    try:
        return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)), tzinfo=UTC)
    except ValueError:
        return None


class JavdbProvider(BaseProvider):
    id = "Javdb"
    host = "javdb.com"
    proxy_key = "Javdb"
    referer = "https://javdb.com/"
    CARD = ".movie-list .item, .grid .item"

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        # Pacing defaults live in the adapter, not only in example.yaml: an existing
        # config without these keys would otherwise hammer javdb into 403s.
        if self.min_interval_seconds <= 0:
            self.min_interval_seconds = JAVDB_MIN_INTERVAL_SECONDS
        if self.jitter_seconds <= 0:
            self.jitter_seconds = JAVDB_JITTER_SECONDS

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search?q={quote(content_code(content))}&f=all"

    async def fetch(self, content: Content, ctx: object) -> MetadataResult:
        """One retry after a throttle pause; throttling stays ``rate_limited``."""

        try:
            return await super().fetch(content, ctx)  # type: ignore[arg-type]
        except ProviderError as exc:
            if not _is_rate_limit(exc):
                raise
        await asyncio.sleep(JAVDB_RATE_LIMIT_PAUSE_SECONDS)
        try:
            return await super().fetch(content, ctx)  # type: ignore[arg-type]
        except ProviderError as exc:
            if _is_rate_limit(exc):
                raise ProviderError("rate_limited", f"javdb: throttled ({exc.code})") from exc
            raise

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        if page.status in RATE_LIMIT_STATUSES:
            raise ProviderError("rate_limited", f"javdb: status {page.status}")
        if len(page.html.strip()) < MIN_PAGE_BYTES:
            # An empty 200 is a throttle stub, not a site redesign.
            raise ProviderError("rate_limited", "javdb: empty response")

        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            text = tree.text(separator=" ", strip=True) or ""
            if any(marker in text for marker in LOGIN_MARKERS):
                # Not a redesign and not throttling: the site wants an account.
                raise ProviderError("login_required", "javdb: search requires login")
            raise ProviderError("structure_changed", "javdb: no movie list")

        wanted = content_number(content)
        for card in cards:
            title = first_text(card, (".video-title", "strong"))
            # Exact number only: ``4906053`` must not answer for ``4906073``.
            if _title_number(title) != wanted:
                continue
            link = card.css_first("a.box, a[href]")
            image = card.css_first(".cover img, img")
            # ``.meta`` on a javdb card is the release date, not a description.
            # Putting it into ``description_ja`` was wrong: it made the card show
            # "2026-11-14" as the plot.
            released = first_text(card, (".meta", ".value"))
            # The title carries the ``【無】/【有】/【薄】`` marker; javdb is not the
            # official site, so only explicit brackets count (never a default).
            censorship, censorship_evidence = infer_censorship({"title": title})
            return self._result(
                content,
                title=title,
                description=None,
                release_date=_parse_meta_date(released),
                official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
                cover=image_src(image, page.url),
                censorship=censorship,
                raw={
                    "provider_url": page.url,
                    "matched": True,
                    "censorship_evidence": censorship_evidence,
                },
            )
        return None


def _is_rate_limit(exc: ProviderError) -> bool:
    """403 / 429, an empty page, or a timeout (``http_error`` without a status)."""

    if exc.code == "rate_limited":
        return True
    if exc.http_status in RATE_LIMIT_STATUSES:
        return True
    return exc.code == "http_error" and exc.http_status is None


def _title_number(title: str | None) -> str | None:
    """The FC2 number a card title claims, as digits."""

    if not title:
        return None
    match = _TITLE_NUMBER_RE.search(title)
    return match.group(1) if match else None
