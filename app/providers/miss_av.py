"""MissAV metadata.

Spec 7.1 priority 6. Selectors are this adapter's own; a page that no
longer matches them raises ``ProviderError`` instead of returning an empty
success. The fixture under ``tests/fixtures/providers/`` is a **constructed
sample** mirroring the expected structure, not a captured page.

The search card carries the title and the cover but usually **no** plot, while the
detail page does. The second request is therefore optional and paid only when the
matched card has no description; it goes through the same direct/Byparr path as the
search page (never a new client, never a direct request around the proxy).
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote

from app.db.models import Content
from app.logging import get_logger
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    Queryable,
    RequestSpec,
    absolute_url,
    clean_text,
    code_matches,
    content_code,
    first_attr,
    first_text,
    meta_content,
)
from app.schemas.metadata import MetadataResult, ProviderContext

_log = get_logger("provider")

CARD_DESCRIPTION_SELECTORS = (".description", "p")
DETAIL_DESCRIPTION_SELECTORS = (".description", ".video-description", "article p")
CANONICAL_SELECTOR = 'link[rel="canonical"]'
#: The detail page is a real page, not a challenge shell: it always has these.
DETAIL_REQUIRED_SELECTORS = ('meta[property="og:title"]', "h1")
#: The detail page is optional, so its own budget is capped instead of doubling
#: the source's whole wall time.
DETAIL_TIMEOUT_SECONDS = 60.0


class MissAvProvider(BaseProvider):
    id = "miss_av"
    host = "missav123.com"
    proxy_key = "miss_av"
    referer = "https://missav123.com/"
    CARD = ".thumbnail, .grid .item, .item"
    REQUIRED_SELECTORS = (CARD,)

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search/{quote(content_code(content))}"

    # -- budget -------------------------------------------------------------

    @property
    def _detail_budget_seconds(self) -> float:
        """One page's budget for the optional detail request."""

        return min(super().fetch_timeout_seconds, DETAIL_TIMEOUT_SECONDS)

    @property
    def fetch_timeout_seconds(self) -> float:
        # The coordinator's hard cap must leave room for the optional second page;
        # otherwise a slow detail fetch would cancel the whole source instead of
        # degrading it to "no description".
        return super().fetch_timeout_seconds + self._detail_budget_seconds

    # -- fetch --------------------------------------------------------------

    async def fetch_extra(
        self, page: ProviderPage, content: Content, ctx: ProviderContext
    ) -> dict[str, str]:
        """Follow the matched card to its detail page when the card has no plot.

        Failures (a challenge, a timeout, a wrong page) are non-fatal: the result
        simply keeps the card's fields and the source still succeeds.
        """

        match = self._matching_card(page, content)
        if match is None:
            return {}
        card, link, _title = match
        if first_text(card, CARD_DESCRIPTION_SELECTORS):
            return {}
        url = absolute_url(link.attributes.get("href") if link is not None else None, page.url)
        if not url:
            return {}
        await self._throttle()
        try:
            loaded = await asyncio.wait_for(
                self._load_page(
                    RequestSpec(url=url, required_selectors=DETAIL_REQUIRED_SELECTORS),
                    referer=page.url,
                    release_title=page.release_title,
                ),
                timeout=self._detail_budget_seconds,
            )
        except (ProviderError, TimeoutError):
            _log.info("provider.detail_unavailable", provider=self.id, url=url)
            return {}
        return {"detail_html": loaded.page.html, "detail_url": loaded.page.url}

    # -- parsing ------------------------------------------------------------

    def _matching_card(
        self, page: ProviderPage, content: Content
    ) -> tuple[Queryable, Queryable, str | None] | None:
        """The card for ``content``, its link node and its title."""

        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "miss_av: no thumbnails")

        for card in cards:
            link = card.css_first("a[href]")
            # On the live search page the cover anchor comes first and has no text;
            # the title lives in its own ``.text-secondary`` anchor further down.
            title_node = card.css_first(".text-secondary") or card.css_first(".title") or link
            title = clean_text(title_node.text(strip=True)) if title_node else None
            if not title and link is not None:
                title = clean_text(link.attributes.get("alt"))
            if not code_matches(title, content):
                continue
            return card, link, title
        return None

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        match = self._matching_card(page, content)
        if match is None:
            return None
        card, link, title = match
        image = card.css_first("img")
        return self._result(
            content,
            title=title,
            description=first_text(card, CARD_DESCRIPTION_SELECTORS)
            or _detail_description(page, content),
            description_quality="summary",
            official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
            cover=absolute_url(image.attributes.get("src") if image else None, page.url),
            raw={"provider_url": page.url, "matched": True},
        )


def _detail_description(page: ProviderPage, content: Content) -> str | None:
    """The plot from the detail page, but only from a page about this number."""

    html_text = page.extra.get("detail_html")
    if not html_text:
        return None
    detail = ProviderPage(
        url=page.extra.get("detail_url") or page.url, html=html_text, status=page.status
    )
    tree = detail.tree
    if not _detail_is_for(tree, content):
        # A redirect to another title (or a stale card link) must never supply the
        # description: the card would show a plot that belongs to a different video.
        _log.info("provider.detail_number_mismatch", provider="miss_av", url=detail.url)
        return None
    return first_text(tree, DETAIL_DESCRIPTION_SELECTORS)


def _detail_is_for(tree: Queryable, content: Content) -> bool:
    return (
        code_matches(first_text(tree, ("h1",)), content)
        or code_matches(meta_content(tree, "og:title"), content)
        or code_matches(first_attr(tree, (CANONICAL_SELECTOR,), "href"), content)
    )
