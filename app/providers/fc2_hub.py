"""FC2 metadata via javten.com (fc2_hub).

Spec 7.1 priority 2. Selectors are this adapter's own; a page that no longer
matches them raises ``ProviderError`` instead of returning an empty success.
The fixture under ``tests/fixtures/providers/`` is a **constructed sample**
mirroring the expected structure, not a captured page.

Measured 2026-11: ``GET https://javten.com/search?kw=CODE`` answers with a 302
to the detail page (the HTTP client follows it). The detail page carries the
fields this adapter needs in ``h1``, ``og:title``, ``og:description``,
``og:image`` and ``link[rel=canonical]``. A page whose ``h1``, ``og:title`` and
``canonical`` all lack the number is a miss, not a result.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    code_matches,
    content_code,
    content_number,
    first_attr,
    first_text,
    meta_content,
)
from app.schemas.metadata import MetadataResult

SEARCH_URL = "https://javten.com/search"
CANONICAL_SELECTOR = 'link[rel="canonical"]'


def _number_in(value: str | None, content: Content) -> bool:
    """True when the bare FC2 number appears in ``value`` (e.g. a canonical URL)."""

    if not value:
        return False
    number = content_number(content)
    if not number.isdigit():
        return False
    return re.search(rf"(?<!\d){re.escape(number)}(?!\d)", value) is not None


class Fc2HubProvider(BaseProvider):
    id = "fc2_hub"
    host = "javten.com"
    proxy_key = "fc2_hub"
    referer = "https://javten.com/"
    #: A detail page always has these; a challenge shell has neither. ``h1`` is
    #: deliberately not required: Cloudflare's interstitial also carries one.
    REQUIRED_SELECTORS = (CANONICAL_SELECTOR, 'meta[property="og:title"]')

    def build_url(self, content: Content) -> str:
        return f"{SEARCH_URL}?kw={quote(content_code(content))}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        heading = first_text(tree, ("h1",))
        og_title = meta_content(tree, "og:title")
        canonical = first_attr(tree, (CANONICAL_SELECTOR,), "href")
        if not (heading or og_title or canonical):
            raise ProviderError(
                "structure_changed", "fc2_hub: detail page has no title or canonical"
            )

        matched = (
            code_matches(heading, content)
            or code_matches(og_title, content)
            or code_matches(canonical, content)
            or _number_in(canonical, content)
        )
        if not matched:
            return None

        return self._result(
            content,
            title=heading or og_title,
            description=meta_content(tree, "og:description"),
            description_quality="summary",
            official_url=absolute_url(canonical, page.url) or page.url,
            cover=absolute_url(meta_content(tree, "og:image"), page.url),
            previews=[],
            raw={"provider_url": page.url, "matched": True},
        )
