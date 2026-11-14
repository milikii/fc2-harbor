"""JavLibrary metadata.

Spec 7.1 priority 7. Selectors are this adapter's own; a page that no
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


class JavlibraryProvider(BaseProvider):
    id = "Javlibrary"
    host = "javlibrary.com"
    proxy_key = "Javlibrary"
    referer = "https://www.javlibrary.com/"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/en/vl_searchbyid.php?keyword={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        title = first_text(tree, ("#video_title a", "#video_title h3", "h1"))
        if title is None:
            # Result list page: pick the first entry, then stop (one request only).
            cards = tree.css(".videos .video, .video")
            if not cards:
                raise ProviderError("structure_changed", "javlibrary: no title and no results")
            link = cards[0].css_first("a[href]")
            title = clean_text(cards[0].text(strip=True))
            return self._result(
                content,
                title=title,
                description=None,
                official_url=absolute_url(link.attributes.get("href") if link else None, page.url),
                cover=None,
                raw={"provider_url": page.url, "matched": True, "list_page": True},
            )

        jacket = tree.css_first("#video_jacket_img")
        cover = absolute_url(jacket.attributes.get("src") if jacket else None, page.url)
        previews = [
            absolute_url(node.attributes.get("src"), page.url)
            for node in tree.css(".previewthumbs img, #preview-video img")
        ]
        info = first_text(tree, ("#video_info", ".item"))
        return self._result(
            content,
            title=title,
            description=info,
            official_url=page.url,
            cover=cover,
            previews=previews,
            raw={"provider_url": page.url, "matched": True},
        )
