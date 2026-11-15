"""7MMTV metadata.

Spec 7.1 priority 4. Real listings are ``div.video`` cards: a lazy cover inside
``figure.video-preview`` and the title in ``h3.video-title > a``. Titles use the
short code form (``fc2-ppv 5006408``), which is why matching goes by number.
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


class MmtvProvider(BaseProvider):
    id = "Mmtv"
    host = "7mmtv.sx"
    proxy_key = "Mmtv"
    referer = "https://7mmtv.sx/"
    CARD = "div.video, .video-item, .item"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/search/?q={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "mmtv: no video cards")

        for card in cards:
            title = first_text(card, (".video-title a", ".video-title", "h3", "a"))
            if not code_matches(title, content):
                continue
            link_node = card.css_first("a[href]")
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".description", "p")),
                description_quality="summary",
                official_url=absolute_url(
                    link_node.attributes.get("href") if link_node else None, page.url
                ),
                cover=image_src(image, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
