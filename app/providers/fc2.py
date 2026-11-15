"""FC2 official metadata — priority 1 (spec 7.1).

Detail page by number: ``https://adult.contents.fc2.com/article/<number>/``.
This is the only provider that is authoritative for title/description/official URL,
so it runs first and everything else only fills gaps.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from app.db.enums import Censorship
from app.db.models import Content
from app.logging import get_logger
from app.network.clients import HttpError
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    clean_text,
    content_number,
    first_attr,
    first_text,
    meta_content,
)
from app.schemas.metadata import MetadataResult, ProviderContext

_log = get_logger("provider.fc2")

#: The article page does not contain the description itself: it embeds
#: ``/widget/article/<number>/description?ac=<token>`` in an iframe. The token is
#: per page load, so it must be read from the article HTML every time.
DESCRIPTION_IFRAME_SELECTOR = "iframe[data-iframe='description']"
#: In the widget document the text lives in a single ``div[data-container]`` that is
#: a direct child of ``body``. The other ``data-container`` nodes are template
#: fragments inside inline scripts.
DESCRIPTION_CONTAINER_SELECTOR = "body > div[data-container]"
#: Nodes that are page furniture rather than description text.
_SKIP_TAGS = frozenset({"script", "style", "noscript", "iframe", "template"})
_HIDDEN_STYLES = (
    "display:none",
    "opacity:0",
    "height:0",
    "visibility:hidden",
)


class Fc2Provider(BaseProvider):
    id = "Fc2"
    host = "adult.contents.fc2.com"
    proxy_key = "Fc2"
    referer = "https://adult.contents.fc2.com/"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/article/{content_number(content)}/"

    async def fetch_extra(
        self, page: ProviderPage, content: Content, ctx: ProviderContext
    ) -> dict[str, str]:
        """Fetch the description widget the article page embeds.

        A failure is logged and ignored: the parser then falls back to the meta
        description, which is shorter but still a usable card.
        """

        source = first_attr(page.tree, (DESCRIPTION_IFRAME_SELECTOR,), "src")
        if not source:
            _log.info("provider.fc2.no_description_iframe", url=page.url)
            return {}
        url = absolute_url(source, page.url)
        if not url:
            return {}
        try:
            response = await self.client.get(
                url,
                headers={"Referer": page.url},
                proxy_url=self.proxy_url,
                proxy_label=self.proxy_key or self.id,
                proxy_region=self.proxy_region,
            )
        except HttpError as exc:
            _log.warning(
                "provider.fc2.description_widget_failed", url=url, status=exc.status
            )
            return {}
        if not 200 <= response.status < 300:
            _log.warning(
                "provider.fc2.description_widget_failed", url=url, status=response.status
            )
            return {}
        return {"description_html": response.text}

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        tree = page.tree
        # og:title is the authoritative title on the real page; the generic h1/h2
        # are site chrome ("コンテンツマーケット", "サービス情報") and must never be
        # used as a fallback.
        raw_title = meta_content(tree, "og:title") or first_text(
            tree, ('h2[itemprop="name"]', ".items_article_Contents h2")
        )
        if not raw_title:
            raise ProviderError("structure_changed", "fc2: no title on article page")
        title = _strip_code_prefix(raw_title)

        raw_description, description_source = _description(page, tree)
        description = _strip_code_prefix(raw_description) if raw_description else None
        # Image sources are protocol-relative on the real page ("//cdn/…").
        cover = absolute_url(
            meta_content(tree, "og:image")
            or first_attr(tree, (".items_article_MainitemThumb img", "main img"), "src"),
            page.url,
        )
        previews = [
            absolute_url(node.attributes.get("src"), page.url)
            for node in tree.css(
                ".items_article_SampleImagesArea img, .sample_image img, "
                ".sample_images img, a[href*='/sample/'] img"
            )
        ]
        released = first_attr(tree, ("time[datetime]", "time"), "datetime")
        release_date = _parse_date(released)
        censorship = _censorship_from_text(tree.text(separator=" ", strip=True))

        return self._result(
            content,
            title=title,
            description=description,
            official_url=f"https://{self.host}/article/{content_number(content)}/",
            censorship=censorship,
            release_date=release_date,
            cover=cover,
            previews=previews,
            raw={
                "provider_url": page.url,
                "matched": True,
                # "full" (widget) / "meta" / "og" — so a short card is explainable.
                "description_source": description_source,
            },
        )


def _description(page: ProviderPage, tree: object) -> tuple[str | None, str | None]:
    """The complete description if available, otherwise the meta fallback.

    Order: description widget (full text, no truncation) → ``meta[name=description]``
    (longer than og) → ``og:description`` (the site truncates it to ~70 characters).
    """

    widget_html = page.extra.get("description_html")
    if widget_html:
        full = _widget_description(widget_html)
        if full:
            return full, "full"
    meta = meta_content(tree, "description")
    if meta:
        return meta, "meta"
    og = meta_content(tree, "og:description")
    if og:
        return og, "og"
    return None, None


def _widget_description(html: str) -> str | None:
    """Read the description container out of the widget document.

    The container mixes real paragraphs with page furniture: an ad block (any
    element carrying a ``class``), inline ``<script>`` tags and tracking nodes hidden
    with ``display:none`` / ``opacity:0``. Those subtrees are skipped, everything
    else is kept verbatim — no truncation happens at the provider layer.
    """

    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(html)
    container = tree.css_first(DESCRIPTION_CONTAINER_SELECTOR)
    if container is None:
        return None
    parts: list[str] = []
    _collect_description(container, parts)
    # Whitespace (including the full-width ideographic space) is folded; the text
    # itself is never shortened.
    return clean_text("".join(parts))


def _collect_description(node: object, parts: list[str]) -> None:
    child = node.child  # type: ignore[attr-defined]
    while child is not None:
        tag = child.tag
        if tag == "-text":
            parts.append(child.text_content)
        elif tag == "br":
            parts.append("\n")
        elif _is_furniture(child):
            pass
        else:
            _collect_description(child, parts)
        child = child.next  # type: ignore[attr-defined]


def _is_furniture(node: object) -> bool:
    tag = node.tag  # type: ignore[attr-defined]
    if tag in _SKIP_TAGS:
        return True
    attributes = node.attributes or {}  # type: ignore[attr-defined]
    if attributes.get("class"):
        return True
    style = (attributes.get("style") or "").replace(" ", "").lower()
    return any(hidden.replace(" ", "") in style for hidden in _HIDDEN_STYLES)


def _strip_code_prefix(title: str) -> str:
    """``FC2-PPV-4761220 作品名`` → ``作品名`` (the code lives in display_code)."""

    match = _CODE_PREFIX_RE.match(title.strip())
    if match is None:
        return title.strip()
    remainder = title.strip()[match.end() :].strip(" 　-–—:：")
    return remainder or title.strip()


_CODE_PREFIX_RE = re.compile(r"^FC2[\s\-_.]*(?:PPV)?[\s\-_.]*\d{3,12}", re.IGNORECASE)


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _censorship_from_text(text: str) -> Censorship | None:
    """Only trust explicit wording; never infer from a filename (spec 7.3)."""

    lowered = text.lower()
    if "無修正" in text or "uncensored" in lowered:
        return Censorship.UNCENSORED
    if "有修正" in text or "修正あり" in text or "censored" in lowered:
        return Censorship.CENSORED
    return None
