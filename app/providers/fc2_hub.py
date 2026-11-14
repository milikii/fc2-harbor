"""FC2 metadata via javten.com (fc2_hub).

Spec 7.1 priority 2. Selectors are this adapter's own; a page that no
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


class Fc2HubProvider(BaseProvider):
    id = "fc2_hub"
    host = "javten.com"
    proxy_key = "fc2_hub"
    referer = "https://javten.com/"
    CARD = ".card, .video-card, article"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search?q={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "fc2_hub: no result cards")

        code = content_code(content).upper()
        for card in cards:
            link_node = card.css_first("a[href]")
            title_node = card.css_first(".card-title, h3, h2, a") or link_node
            title = clean_text(title_node.text(strip=True)) if title_node else None
            if not title or code not in title.upper():
                continue
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".card-text", ".description", "p")),
                official_url=absolute_url(
                    link_node.attributes.get("href") if link_node else None, page.url
                ),
                cover=absolute_url(image.attributes.get("src") if image else None, page.url),
                previews=[],
                raw={"provider_url": page.url, "matched": True},
            )
        return None
