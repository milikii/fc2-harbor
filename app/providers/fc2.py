"""FC2 official metadata — priority 1 (spec 7.1).

Detail page by number: ``https://adult.contents.fc2.com/article/<number>/``.
Authoritative for the title and the official URL. The description here is only a
**teaser** (the site truncates ``og:description`` to ~70 characters and
``meta[name=description]`` to a few hundred) and is marked ``summary`` so a
provider with real body text can replace it (docs/deviations.md §34/§35).
"""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime

from app.db.enums import Censorship
from app.db.models import Content
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    content_number,
    first_attr,
    first_text,
    infer_censorship,
    meta_content,
)
from app.schemas.metadata import MetadataResult


class Fc2Provider(BaseProvider):
    id = "Fc2"
    host = "adult.contents.fc2.com"
    proxy_key = "Fc2"
    referer = "https://adult.contents.fc2.com/"

    def build_url(self, content: Content) -> str:
        return f"https://{self.host}/article/{content_number(content)}/"

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

        # The official page only publishes a teaser: og:description is cut to ~70
        # characters and meta[name=description] to a few hundred. The full text sits
        # behind an iframe widget, which the user explicitly rejected ("no need for
        # the official site"), so this is a *summary* by definition and a provider
        # with real body text may replace it (docs/deviations.md §35).
        meta_description = meta_content(tree, "description")
        og_description = meta_content(tree, "og:description")
        raw_description = meta_description or og_description
        description = _strip_code_prefix(raw_description) if raw_description else None
        description_source = (
            "meta" if meta_description else ("og" if og_description else None)
        )
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
        # Censorship: the site's own product tags first (data-tag="…"), then the
        # bracketed short form in the titles, then "censored" as the FC2 default —
        # this is what the user's old system did, and why it never showed "unknown".
        # The page was fetched successfully, so the default is a statement, not a
        # guess; a removed article never reaches this code (it 404s).
        # Seller description text is deliberately never used (torrentmate rule).
        official_tags = _official_tags(page.html)
        censorship, censorship_evidence = censorship_from_tags(
            official_tags, title, page.release_title
        )

        return self._result(
            content,
            title=title,
            description=description,
            description_quality="summary" if description else None,
            official_url=f"https://{self.host}/article/{content_number(content)}/",
            censorship=censorship,
            release_date=release_date,
            cover=cover,
            previews=previews,
            raw={
                "provider_url": page.url,
                "matched": True,
                # "meta" (longer teaser) / "og" (site-truncated to ~70 chars) / None.
                "description_source": description_source,
                # Which wording produced the tag, and from which field.
                "censorship_evidence": censorship_evidence,
                # The site's own product tags, for later re-scoring without HTTP.
                "tags": official_tags,
            },
        )


#: The site marks each product tag as ``data-tag="…"``.
_TAG_RE = re.compile(r'data-tag="([^"]+)"', re.IGNORECASE)


def _official_tags(html_text: str) -> list[str]:
    """The product tags shown on the article page (deduplicated, entity-decoded)."""

    seen: set[str] = set()
    tags: list[str] = []
    for raw in _TAG_RE.findall(html_text or ""):
        tag = html.unescape(raw).strip()
        if tag and tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags


def censorship_from_tags(
    tags: list[str], title: str | None, release_title: str | None
) -> tuple[Censorship, dict[str, object]]:
    """Official tags → bracketed title forms → "censored" as the FC2 default."""

    tag_text = " ".join(tags)
    if "無修正" in tag_text:
        return Censorship.UNCENSORED, {"source": "official_tags", "tags": tags}
    if "薄モザイク" in tag_text or "薄消し" in tag_text:
        return Censorship.THIN, {"source": "official_tags", "tags": tags}

    inferred, evidence = infer_censorship({"title": title, "release_title": release_title})
    if inferred is not None:
        evidence = {**evidence, "source": "title"}
        return inferred, evidence

    return Censorship.CENSORED, {
        "source": "default",
        "tags": tags,
        "reason": "FC2 products are censored unless the official tags say otherwise",
    }


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
