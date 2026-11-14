"""Nyaa page fallback — the last step (spec 7.2 rule 10).

Spec 7.1 priority 10. Selectors are this adapter's own; a page that no
longer matches them raises ``ProviderError`` instead of returning an empty
success. The fixture under ``tests/fixtures/providers/`` is a **constructed
sample** mirroring the expected structure, not a captured page.
"""

from __future__ import annotations

from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    clean_text,
    first_text,
    meta_content,
)
from app.schemas.metadata import MetadataResult


class NyaaFallbackProvider(BaseProvider):
    id = "Nyaa"
    host = "sukebei.nyaa.si"
    proxy_key = "nyaa"

    def build_url(self, content: Content) -> str:
        raise ProviderError("missing_release", "nyaa fallback needs the release url, not a content")

    def url_for_release(self, release_url: str | None, item_id: str | None) -> str:
        if release_url and "sukebei.nyaa.si" in release_url:
            return release_url
        if item_id:
            return f"https://{self.host}/view/{item_id}"
        raise ProviderError("missing_release", "no release url or item id for nyaa fallback")

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        title = first_text(tree, ("#torrent-title", "h3.panel-title", "h1")) or meta_content(
            tree, "og:title"
        )
        if not title:
            raise ProviderError("structure_changed", "nyaa: no torrent title")

        body = tree.css_first(".panel-body, #torrent-description")
        description = clean_text(body.text(separator=" ", strip=True)) if body else None
        images = [
            absolute_url(node.attributes.get("src"), page.url)
            for node in tree.css(".panel-body img, #torrent-description img")
        ]
        cover = meta_content(tree, "og:image")
        return self._result(
            content,
            title=title,
            description=description,
            official_url=page.url,
            cover=cover,
            previews=images,
            raw={"provider_url": page.url, "matched": True, "fallback": True},
        )
