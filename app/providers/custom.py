"""Configuration-defined provider — add a site without a code change.

The nine built-in adapters stay (spec 7.1 fixes their order). This one lets an
operator who found a working site add it in YAML::

    provider:
      custom:
        - name: some_site
          search_url: "https://x.com/search?q={code}"
          detail_url: "https://x.com/v/{id}"     # optional
          selectors: {card: ".item", title: ".title", link: "a[href]",
                      cover: "img[data-src]", description: ".desc",
                      previews: ".gallery img"}

Behaviour:

* the search page is parsed directly — most such sites need no detail page, and
  several require a login there;
* if ``detail_url`` is configured, the matched card's link is followed once for the
  fields the search page does not carry;
* a page whose card selector matches nothing raises ``structure_changed`` instead of
  pretending the content does not exist — the operator must notice a broken site;
* every request goes through the same browser headers, cookie warm-up and pacing as
  the built-in providers.
"""

from __future__ import annotations

import re
from urllib.parse import quote, urlsplit

from app.config import CustomProviderConfig
from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    code_matches,
    content_code,
    first_text,
    image_src,
)
from app.schemas.metadata import MetadataResult, ProviderContext

_ID_RE = re.compile(r"/([^/?#]+)/?(?:[?#].*)?$")


class CustomProvider(BaseProvider):
    """One config-defined site. ``id`` is the configured name."""

    def __init__(self, *, config: CustomProviderConfig, **kwargs: object) -> None:
        self.config = config
        self.id = config.name
        self.host = (urlsplit(config.search_url).hostname or "").lower()
        self.proxy_key = config.name
        super().__init__(
            headers=config.headers,
            warmup_url=config.warmup_url,
            min_interval_seconds=config.min_interval_seconds,
            jitter_seconds=config.jitter_seconds,
            **kwargs,  # type: ignore[arg-type]
        )

    # -- URL ---------------------------------------------------------------

    def build_url(self, content: Content) -> str:
        return self.config.search_url.format(code=quote(content_code(content)))

    def _detail_url(self, href: str | None) -> str | None:
        if not self.config.detail_url or not href:
            return None
        match = _ID_RE.search(href)
        if match is None:
            return None
        return self.config.detail_url.format(id=match.group(1))

    # -- matching ----------------------------------------------------------

    def _matching_card(self, page: ProviderPage, content: Content) -> tuple[object, str] | None:
        tree = page.tree
        cards = tree.css(self.config.selectors.card)
        if not cards:
            raise ProviderError(
                "structure_changed",
                f"{self.id}: no card matched {self.config.selectors.card!r}",
            )
        for card in cards:
            title = first_text(card, (self.config.selectors.title,))
            if code_matches(title, content):
                link = card.css_first(self.config.selectors.link)
                href = link.attributes.get("href") if link else None
                return card, absolute_url(href, page.url) or ""
        return None

    async def fetch_extra(
        self, page: ProviderPage, content: Content, ctx: ProviderContext
    ) -> dict[str, str]:
        """Follow the matched card's link when a detail template is configured."""

        if not self.config.detail_url:
            return {}
        match = self._matching_card(page, content)
        if match is None:
            return {}
        _card, href = match
        url = self._detail_url(href)
        if url is None:
            return {}
        try:
            response = await self.client.get(
                url,
                headers=self._headers(url),
                proxy_url=self.proxy_url,
                proxy_label=self.proxy_key,
                proxy_region=self.proxy_region,
            )
        except Exception:
            return {}
        if not 200 <= response.status < 300:
            return {}
        return {"detail_html": response.text, "detail_url": url}

    def _headers(self, url: str) -> dict[str, str]:
        from app.network.browser_headers import provider_headers

        return provider_headers(self.host, referer=url, overrides=self.headers)

    # -- parsing -----------------------------------------------------------

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        match = self._matching_card(page, content)
        if match is None:
            return None
        card, href = match
        selectors = self.config.selectors
        title = first_text(card, (selectors.title,))

        detail_html = page.extra.get("detail_html")
        detail_url = page.extra.get("detail_url") or href or page.url
        if detail_html:
            detail = ProviderPage(url=detail_url, html=detail_html, status=page.status)
            title = title or first_text(detail.tree, (selectors.title,))
            description = _optional_text(detail, selectors.description)
            cover = _optional_cover(detail, selectors.cover, detail.url)
            previews = _optional_previews(detail, selectors.previews, detail.url)
        else:
            description = _optional_text(page, selectors.description)
            cover = _optional_cover(page, selectors.cover, page.url)
            previews = _optional_previews(page, selectors.previews, page.url)

        return self._result(
            content,
            title=title,
            description=description,
            description_quality="summary" if description else None,
            official_url=href or page.url,
            cover=cover,
            previews=previews,
            raw={
                "provider_url": page.url,
                "matched": True,
                "custom": True,
                "detail_fetched": bool(detail_html),
            },
        )


def _optional_text(page: ProviderPage, selector: str | None) -> str | None:
    if not selector:
        return None
    return first_text(page.tree, (selector,))


def _optional_cover(page: ProviderPage, selector: str | None, base: str) -> str | None:
    if not selector:
        return None
    return image_src(page.tree.css_first(selector), base)


def _optional_previews(page: ProviderPage, selector: str | None, base: str) -> list[str]:
    if not selector:
        return []
    urls: list[str] = []
    for node in page.tree.css(selector):
        url = image_src(node, base)
        if url:
            urls.append(url)
    return urls


def build_custom_providers(
    settings: object, *, client: object, router: object
) -> list[CustomProvider]:
    """Instantiate every enabled ``provider.custom`` entry, in config order."""

    from app.config import Settings
    from app.network.clients import HttpClient
    from app.network.proxy import ProxyRouter

    assert isinstance(settings, Settings)
    assert isinstance(client, HttpClient)
    assert isinstance(router, ProxyRouter)
    providers: list[CustomProvider] = []
    for entry in settings.provider.custom:
        if not entry.enabled:
            continue
        target = router.for_provider(entry.proxy)
        providers.append(
            CustomProvider(
                config=entry,
                client=client,
                proxy_url=target.url,
                timeout_seconds=entry.timeout_seconds,
                proxy_region=target.region,
            )
        )
    return providers
