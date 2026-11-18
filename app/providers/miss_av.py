"""MissAV metadata.

Spec 7.1 priority 6. Selectors are this adapter's own; a page that no
longer matches them raises ``ProviderError`` instead of returning an empty
success. The fixture under ``tests/fixtures/providers/`` is a **constructed
sample** mirroring the expected structure, not a captured page.
"""

from __future__ import annotations

from urllib.parse import quote

from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    clean_text,
    code_matches,
    content_code,
    first_text,
)
from app.schemas.metadata import MetadataResult


class MissAvProvider(BaseProvider):
    id = "miss_av"
    host = "missav123.com"
    proxy_key = "miss_av"
    referer = "https://missav123.com/"
    CARD = ".thumbnail, .grid .item, .item"
    REQUIRED_SELECTORS = (CARD,)

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search/{quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
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
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".description", "p")),
                description_quality="summary",
                official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
                cover=absolute_url(image.attributes.get("src") if image else None, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
