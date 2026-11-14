"""JavDB metadata.

Spec 7.1 priority 3. Real results are ``.movie-list .item > a.box`` with a
``.cover img`` and a ``.video-title`` that starts with the code in a ``<strong>``
(``FC2-5000075`` short form). JavDB indexes FC2 releases, so it is one of the more
useful sources; a miss is a normal outcome, not a failure.
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

        for card in cards:
            title = first_text(card, (".video-title", "strong"))
            if not code_matches(title, content):
                continue
            link = card.css_first("a.box, a[href]")
            image = card.css_first(".cover img, img")
            return self._result(
                content,
                title=title,
                description=first_text(card, (".meta", ".value")),
                official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
                cover=image_src(image, page.url),
                raw={"provider_url": page.url, "matched": True},
            )
        return None
