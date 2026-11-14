"""FreeJAVBT metadata.

Spec 7.1 priority 5. Real listings are ``.video-list-item > .card`` with a lazy
cover (``data-src``) and ``h5.card-title`` (code) + ``p.card-text`` (title), so
matching uses the card text and the cover prefers the lazy attribute. A page
without those anchors raises ``ProviderError`` instead of returning empty success.
"""

from __future__ import annotations

from urllib.parse import quote

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
from app.schemas.metadata import MetadataResult


class FreejavbtProvider(BaseProvider):
    id = "freejavbt"
    host = "freejavbt.com"
    proxy_key = "freejavbt"
    referer = "https://freejavbt.com/"
    CARD = ".video-list-item, .card, article"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/?s={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "freejavbt: no cards")

        for card in cards:
            title = first_text(card, ("p.card-text", ".card-title", ".entry-title", "h2"))
            card_text = card.text(separator=" ", strip=True)
            if not code_matches(title, content) and not code_matches(card_text, content):
                continue
            link_node = card.css_first("a[href]")
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, ("p.card-text", ".entry-summary", "p")),
                official_url=absolute_url(
                    link_node.attributes.get("href") if link_node else None, page.url
                ),
                cover=image_src(image, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
