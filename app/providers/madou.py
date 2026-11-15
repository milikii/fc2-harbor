"""Madou metadata (last of the nine).

Spec 7.1 priority 9. The site answers a search with no results as an HTTP 200 page
whose body is a 404 panel, which must be treated as a miss, not as a broken page.
Low hit rate for FC2, kept because spec 7.1 forbids reordering by hit rate.
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


class MadouProvider(BaseProvider):
    id = "Madou"
    host = "madou.club"
    proxy_key = "Madou"
    referer = "https://madou.club/"
    CARD = "article, .post, .item"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/?s={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        text = tree.text(separator=" ", strip=True)
        if "404" in text[:400] or tree.css_first(".f404") is not None:
            # "No result" is served as a 404 panel with status 200.
            return None

        cards = tree.css(self.CARD)
        if not cards:
            raise ProviderError("structure_changed", "madou: no articles")

        for card in cards:
            title = first_text(card, (".entry-title a", ".entry-title", "h2 a", "h2"))
            if not code_matches(title, content):
                continue
            link_node = card.css_first(".entry-title a, h2 a, a[href]")
            image = card.css_first("img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".entry-summary", ".entry-content", "p")),
                description_quality="summary",
                official_url=absolute_url(
                    link_node.attributes.get("href") if link_node else None, page.url
                ),
                cover=image_src(image, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
