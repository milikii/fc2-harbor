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
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    absolute_url,
    content_number,
    first_attr,
    first_text,
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

        raw_description = meta_content(tree, "og:description", "description") or first_text(
            tree, (".section.user_description", ".item_description", ".product_description")
        )
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
            raw={"provider_url": page.url, "matched": True},
        )


def _strip_code_prefix(title: str) -> str:
    """``FC2-PPV-4761220 作品名`` → ``作品名`` (the code lives in display_code)."""

    match = _CODE_PREFIX_RE.match(title.strip())
    if match is None:
        return title.strip()
    remainder = title.strip()[match.end() :].strip(" 　-–—:：")
    return remainder or title.strip()


_CODE_PREFIX_RE = re.compile(r"^FC2[\s\-_.]*PPV?[\s\-_.]*\d{3,12}", re.IGNORECASE)


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
