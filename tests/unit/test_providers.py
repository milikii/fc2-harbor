"""Provider adapters — spec 7.4 / 22.2.

One case per provider against its own fixture: the happy path, the "no matching
entry" path and the "structure changed" path. Fixtures are constructed samples
(see tests/fixtures/providers/README.md); they prove selector and merge behaviour,
not that a live site still looks like this.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.db.enums import Censorship, ContentType
from app.db.models import Content
from app.network.clients import HttpClient
from app.providers.avsox import AvsoxProvider
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    code_matches,
)
from app.providers.fc2 import Fc2Provider
from app.providers.fc2_hub import Fc2HubProvider
from app.providers.freejavbt import FreejavbtProvider
from app.providers.javdb import JavdbProvider
from app.providers.javlibrary import JavlibraryProvider
from app.providers.madou import MadouProvider
from app.providers.miss_av import MissAvProvider
from app.providers.mmtv import MmtvProvider
from app.providers.nyaa_fallback import NyaaFallbackProvider
from app.schemas.metadata import ProviderContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "providers"


def make_content(number: str = "1234567") -> Content:
    return Content(
        content_type=ContentType.FC2,
        canonical_key=f"fc2_ppv:{number}",
        display_code=f"FC2-PPV-{number}",
    )


def provider(cls: type[BaseProvider]) -> BaseProvider:
    return cls(client=HttpClient(), proxy_url=None, timeout_seconds=5)


def page(name: str, url: str) -> ProviderPage:
    return ProviderPage(url=url, html=(FIXTURES / name).read_text(encoding="utf-8"), status=200)


CASES = [
    (Fc2Provider, "fc2.html", "https://adult.contents.fc2.com/article/1234567/", True, 2),
    (Fc2HubProvider, "fc2_hub.html", "https://javten.com/search?q=FC2-PPV-1234567", True, 0),
    (JavdbProvider, "javdb.html", "https://javdb.com/search?q=FC2-PPV-1234567&f=all", True, 0),
    (MmtvProvider, "mmtv.html", "https://7mmtv.sx/search/?q=FC2-PPV-1234567", True, 0),
    (FreejavbtProvider, "freejavbt.html", "https://freejavbt.com/?s=FC2-PPV-1234567", True, 0),
    (MissAvProvider, "miss_av.html", "https://missav123.com/search/FC2-PPV-1234567", True, 0),
    (JavlibraryProvider, "javlibrary.html", "https://javlibrary.com/en/vl_searchbyid.php", True, 2),
    (AvsoxProvider, "avsox.html", "https://avsox.click/en/search/FC2-PPV-1234567", True, 0),
    (MadouProvider, "madou.html", "https://madou.club/?s=FC2-PPV-1234567", True, 0),
]


@pytest.mark.parametrize(("cls", "fixture", "url", "expect_cover", "preview_count"), CASES)
def test_provider_parses_its_fixture(
    cls: type[BaseProvider],
    fixture: str,
    url: str,
    expect_cover: bool,
    preview_count: int,
) -> None:
    content = make_content()
    result = provider(cls).parse(page(fixture, url), content)

    assert result is not None
    assert result.provider == cls.id
    assert result.content_key == content.canonical_key
    assert result.title_ja
    assert result.official_url
    assert (result.cover is not None) is expect_cover
    assert len(result.previews) == preview_count
    for candidate in result.previews:
        assert candidate.url.startswith("http")
        assert candidate.provider == cls.id


def test_madou_404_page_is_a_miss_not_a_failure() -> None:
    result = provider(MadouProvider).parse(
        page("madou_not_found.html", "https://madou.club/?s=FC2-1234567"), make_content()
    )
    assert result is None


def test_freejavbt_uses_lazy_image_and_card_text() -> None:
    result = provider(FreejavbtProvider).parse(
        page("freejavbt.html", "https://freejavbt.com/?s=FC2-1234567"), make_content()
    )
    assert result is not None
    assert result.cover is not None
    assert result.cover.url == "https://cdn.example.com/covers/1234567.jpg"
    assert result.title_ja and "サンプル作品" in result.title_ja


def test_mmtv_uses_video_container_and_lazy_cover() -> None:
    result = provider(MmtvProvider).parse(
        page("mmtv.html", "https://7mmtv.sx/search/?q=FC2-1234567"), make_content()
    )
    assert result is not None
    assert result.cover is not None
    assert result.cover.url == "https://n3.examplecdn.sx/uncensored/m/443541.jpg"


def test_javdb_reads_box_link_and_cover() -> None:
    result = provider(JavdbProvider).parse(
        page("javdb.html", "https://javdb.com/search?q=FC2-1234567&f=all"), make_content()
    )
    assert result is not None
    assert result.official_url == "https://javdb.com/v/abcd12"
    assert result.cover is not None
    assert result.cover.url == "https://c0.jdbstatic.com/covers/ab/abcd12.jpg"


def test_fc2_extracts_official_fields() -> None:
    result = provider(Fc2Provider).parse(
        page("fc2.html", "https://adult.contents.fc2.com/article/1234567/"), make_content()
    )

    assert result is not None
    # The display code is stripped; site chrome ("コンテンツマーケット") never wins.
    assert result.title_ja == "サンプル作品タイトル"
    assert result.description_ja and "作品" in result.description_ja
    assert result.official_url == "https://adult.contents.fc2.com/article/1234567/"


def test_fc2_absolutises_protocol_relative_images() -> None:
    result = provider(Fc2Provider).parse(
        page("fc2.html", "https://adult.contents.fc2.com/article/1234567/"), make_content()
    )

    assert result is not None
    assert result.cover is not None
    assert result.cover.url == "https://storage201000.contents.fc2.com/file/cover.jpg"
    assert all(
        item.url.startswith("https://contents-thumbnail2.fc2.com/") for item in result.previews
    )


def test_fc2_censorship_only_from_explicit_wording() -> None:
    """Spec 7.3: never infer the tag from a filename or a guess."""

    html = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        "</head><body><p>この作品は無修正です。</p></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(url="https://adult.contents.fc2.com/article/1234567/", html=html, status=200),
        make_content(),
    )
    assert result is not None
    assert result.censorship is Censorship.UNCENSORED


def test_javlibrary_collects_previews() -> None:
    result = provider(JavlibraryProvider).parse(
        page("javlibrary.html", "https://www.javlibrary.com/en/vl_searchbyid.php"),
        make_content(),
    )
    assert result is not None
    assert len(result.previews) == 2
    assert result.cover is not None and result.cover.role == "cover"


@pytest.mark.parametrize(("cls", "fixture", "url", "_cover", "_previews"), CASES)
def test_missing_anchors_raise_structure_changed(
    cls: type[BaseProvider],
    fixture: str,
    url: str,
    _cover: bool,
    _previews: int,
) -> None:
    empty = ProviderPage(url=url, html="<html><body><p>nothing here</p></body></html>", status=200)
    with pytest.raises(ProviderError) as excinfo:
        provider(cls).parse(empty, make_content())
    assert excinfo.value.code == "structure_changed"


@pytest.mark.parametrize(
    ("cls", "fixture", "url"),
    [
        (Fc2HubProvider, "fc2_hub.html", "https://javten.com/search?q=FC2-PPV-7654321"),
        (JavdbProvider, "javdb.html", "https://javdb.com/search?q=FC2-PPV-7654321&f=all"),
        (MmtvProvider, "mmtv.html", "https://7mmtv.sx/search/?q=FC2-PPV-7654321"),
        (FreejavbtProvider, "freejavbt.html", "https://freejavbt.com/?s=FC2-PPV-7654321"),
        (MissAvProvider, "miss_av.html", "https://missav123.com/search/FC2-PPV-7654321"),
        (AvsoxProvider, "avsox.html", "https://avsox.click/en/search/FC2-PPV-7654321"),
        (MadouProvider, "madou.html", "https://madou.club/?s=FC2-PPV-7654321"),
    ],
)
def test_no_matching_entry_returns_none(cls: type[BaseProvider], fixture: str, url: str) -> None:
    """A fetched page without our number is a miss, not a failure."""

    assert provider(cls).parse(page(fixture, url), make_content("7654321")) is None


def test_nyaa_fallback_parses_view_page() -> None:
    fallback = provider(NyaaFallbackProvider)
    url = fallback.url_for_release("https://sukebei.nyaa.si/view/4761220", "4761220")
    assert url == "https://sukebei.nyaa.si/view/4761220"

    result = fallback.parse(page("nyaa_view.html", url), make_content())
    assert result is not None
    assert result.title_ja and "FC2-1234567" in result.title_ja
    assert result.description_ja and "作品" in result.description_ja
    assert len(result.previews) == 2
    assert result.cover is not None


def test_nyaa_fallback_builds_url_from_item_id() -> None:
    fallback = provider(NyaaFallbackProvider)
    assert fallback.url_for_release(None, "4761220") == "https://sukebei.nyaa.si/view/4761220"
    with pytest.raises(ProviderError):
        fallback.url_for_release(None, None)


def _retitle(html: str, code: str) -> str:
    """Replace any FC2 code token in a fixture with ``code``."""

    import re

    return re.sub(r"FC2[\s\-_.]*(?:PPV)?[\s\-_.]*1234567", code, html, flags=re.IGNORECASE)


CODE_MATCHING_CASES = [
    (Fc2HubProvider, "fc2_hub.html", "https://javten.com/search?q=FC2-PPV-1234567"),
    (JavdbProvider, "javdb.html", "https://javdb.com/search?q=FC2-PPV-1234567&f=all"),
    (MmtvProvider, "mmtv.html", "https://7mmtv.sx/search/?q=FC2-PPV-1234567"),
    (FreejavbtProvider, "freejavbt.html", "https://freejavbt.com/?s=FC2-PPV-1234567"),
    (MissAvProvider, "miss_av.html", "https://missav123.com/search/FC2-PPV-1234567"),
    (AvsoxProvider, "avsox.html", "https://avsox.click/en/search/FC2-PPV-1234567"),
    (MadouProvider, "madou.html", "https://madou.club/?s=FC2-PPV-1234567"),
]


@pytest.mark.parametrize(("cls", "fixture", "url"), CODE_MATCHING_CASES)
@pytest.mark.parametrize(
    "title_code",
    ["FC2-PPV-1234567", "FC2-1234567", "FC2PPV1234567", "FC2 PPV 1234567"],
)
def test_every_provider_matches_short_and_long_titles(
    cls: type[BaseProvider], fixture: str, url: str, title_code: str
) -> None:
    """Real titles write the number as FC2-<n> far more often than FC2-PPV-<n>."""

    # Sites write the number in different shapes, so replace the whole token.
    html = _retitle((FIXTURES / fixture).read_text(encoding="utf-8"), title_code)
    result = provider(cls).parse(ProviderPage(url=url, html=html, status=200), make_content())
    assert result is not None, f"{cls.id} missed a title written as {title_code}"
    assert result.title_ja


@pytest.mark.parametrize(("cls", "fixture", "url"), CODE_MATCHING_CASES)
def test_provider_does_not_match_a_different_number(
    cls: type[BaseProvider], fixture: str, url: str
) -> None:
    html = _retitle((FIXTURES / fixture).read_text(encoding="utf-8"), "FC2-7654321")
    assert provider(cls).parse(ProviderPage(url=url, html=html, status=200), make_content()) is None


def test_code_matches_accepts_all_documented_formats() -> None:
    content = make_content("5000075")
    for title in (
        "FC2-PPV-5000075 タイトル",
        "FC2-5000075【素人初撮り】",
        "FC2PPV5000075",
        "FC2 PPV 5000075",
        "FC2_5000075",
    ):
        assert code_matches(title, content), title

    for title in ("FC2-50000751", "FC2-1234567", "AFC2-5000075", "", None):
        assert not code_matches(title, content), title


async def test_http_404_is_a_miss() -> None:
    """A search-by-code page answers 404 when the site has no such entry."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    client = HttpClient(transport=httpx.MockTransport(handler), retries=0)
    instance = MadouProvider(client=client, proxy_url=None, timeout_seconds=5)
    try:
        with pytest.raises(ProviderError) as excinfo:
            await instance.fetch(make_content(), ProviderContext(canonical_key="fc2_ppv:1234567"))
    finally:
        await client.aclose()
    assert excinfo.value.code == "not_found"


def test_provider_ids_match_priority_order() -> None:
    ids = [cls.id for cls, *_ in CASES]
    assert ids == [
        "Fc2",
        "fc2_hub",
        "Javdb",
        "Mmtv",
        "freejavbt",
        "miss_av",
        "Javlibrary",
        "avsox",
        "Madou",
    ]
