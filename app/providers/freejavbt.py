"""FreeJAVBT metadata.

Spec 7.1 priority 5. Selectors are this adapter's own; a page that no
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
    content_code,
    first_text,
)
from app.schemas.metadata import MetadataResult


class FreejavbtProvider(BaseProvider):
    id = "freejavbt"
    host = "freejavbt.com"
    proxy_key = "freejavbt"
    referer = "https://freejavbt.com/"
    CARD = "article, .post"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/?s={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "freejavbt: no articles")

        code = content_code(content).upper()
        for card in cards:
            title_node = card.css_first(".entry-title, h2, h3")
            link_node = card.css_first(".entry-title a, h2 a, a[href]")
            title = clean_text(title_node.text(strip=True)) if title_node else None
            if not title or code not in title.upper():
                continue
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".entry-summary", ".entry-content", "p")),
                official_url=absolute_url(
                    link_node.attributes.get("href") if link_node else None, page.url
                ),
                cover=absolute_url(image.attributes.get("src") if image else None, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
