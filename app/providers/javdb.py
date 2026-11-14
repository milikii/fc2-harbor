"""JavDB metadata.

Spec 7.1 priority 3. Selectors are this adapter's own; a page that no
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


class JavdbProvider(BaseProvider):
    id = "Javdb"
    host = "javdb.com"
    proxy_key = "Javdb"
    referer = "https://javdb.com/"
    CARD = ".movie-list .item, .grid .item"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search?q={quote(content_code(content))}&f=all"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "javdb: no movie list")

        code = content_code(content).upper()
        for card in cards:
            title_node = card.css_first(".video-title, strong")
            title = clean_text(title_node.text(strip=True)) if title_node else None
            if not title or code not in title.upper():
                continue
            link = card.css_first("a[href]")
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".meta", ".value", ".tags")),
                official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
                cover=absolute_url(image.attributes.get("src") if image else None, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
