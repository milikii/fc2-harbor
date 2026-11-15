"""Nyaa page fallback — the last step (spec 7.2 rule 10).

Spec 7.1 priority 10. This is the safety net when the official site is down or the
content was removed: the torrent we already track still has a title, an uploader
description and often images.

Two real-world details the adapter has to handle:

* an RSS ``link`` is ``/download/<id>.torrent``, i.e. a **torrent file**, not an
  HTML page — requesting it yields bencode and no title, so it must be rewritten to
  ``/view/<id>``;
* ``og:image`` on a Nyaa page is the site's default avatar, never a cover.
"""

from __future__ import annotations

import re

from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    clean_text,
    first_text,
    meta_content,
)
from app.schemas.metadata import MetadataResult

#: ``/download/4760732.torrent`` → ``/view/4760732``.
_DOWNLOAD_RE = re.compile(r"/download/(?P<id>\d+)", re.IGNORECASE)
#: A bare image URL pasted into the uploader's description.
_BARE_IMAGE_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)


class NyaaFallbackProvider(BaseProvider):
    id = "Nyaa"
    host = "sukebei.nyaa.si"
    proxy_key = "nyaa"

    def build_url(self, content: Content) -> str:
        raise ProviderError("missing_release", "nyaa fallback needs the release url, not a content")

    def url_for_release(self, release_url: str | None, item_id: str | None) -> str:
        """The HTML view URL for a release.

        RSS links point at ``/download/<id>.torrent``; fetching that returns a
        torrent file and the parser then finds no title at all.
        """

        download = _DOWNLOAD_RE.search(release_url or "")
        if download is not None:
            return f"https://{self.host}/view/{download.group('id')}"
        if release_url and "sukebei.nyaa.si" in release_url:
            return release_url
        if item_id:
            return f"https://{self.host}/view/{item_id}"
        raise ProviderError("missing_release", "no release url or item id for nyaa fallback")

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        title = _title_from_page(tree, page)
        if not title:
            raise ProviderError("structure_changed", "nyaa: no torrent title")

        # ``#torrent-description`` is the uploader's post; the first ``.panel-body``
        # is the metadata table ("Category:… Date:… Info hash:…") and must never be
        # shown as the plot.
        body = tree.css_first("#torrent-description") or tree.css_first(".panel-body")
        description = clean_text(body.text(separator=" ", strip=True)) if body else None
        images = _description_images(tree, body, page.url)
        # ``og:image`` is the site avatar; a real cover only comes from the post.
        cover = images[0] if images else None
        return self._result(
            content,
            title=title,
            description=description,
            # The torrent description is the uploader's body text, not a teaser.
            description_quality="full" if description else None,
            official_url=page.url,
            cover=cover,
            previews=images[1:],
            raw={"provider_url": page.url, "matched": True, "fallback": True},
        )


def _title_from_page(tree: object, page: ProviderPage) -> str | None:
    """Page title first, then the release title we already stored.

    The release title is the reason this fallback can always produce a card: the
    torrent exists, so its name exists, even when the page markup changes.
    """

    title = first_text(tree, ("#torrent-title", "h3.panel-title", "h1"))
    if not title:
        og_title = meta_content(tree, "og:title")
        if og_title:
            # "… :: Sukebei" is the site suffix.
            title = re.sub(r"\s*::\s*Sukebei\s*$", "", og_title, flags=re.IGNORECASE)
    if not title:
        title = clean_text(page.release_title)
    return title


def _description_images(tree: object, body: object, base: str) -> list[str]:
    """Images from ``<img>`` tags plus bare image URLs pasted in the text."""

    urls: list[str] = []
    for node in tree.css(".panel-body img, #torrent-description img"):  # type: ignore[attr-defined]
        url = absolute_url(node.attributes.get("src"), base)
        if url:
            urls.append(url)
    if body is not None:
        text = body.text(separator=" ", strip=True) or ""  # type: ignore[attr-defined]
        for match in _BARE_IMAGE_RE.finditer(text):
            urls.append(match.group(0))
    seen: set[str] = set()
    ordered: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        ordered.append(url)
    return ordered
