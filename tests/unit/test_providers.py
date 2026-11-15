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
    # The first description image becomes the cover; the rest stay previews.
    assert result.cover is not None
    assert result.cover.url == "https://sukebei.nyaa.si/img/n1.jpg"
    assert [item.url for item in result.previews] == ["https://sukebei.nyaa.si/img/n2.jpg"]


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


async def test_non_200_success_status_is_accepted() -> None:
    """A 2xx other than 200 is still a successful fetch."""

    body = (FIXTURES / "madou.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(203, content=body, headers={"content-type": "text/html"})

    client = HttpClient(transport=httpx.MockTransport(handler), retries=0)
    instance = MadouProvider(client=client, proxy_url=None, timeout_seconds=5)
    try:
        result = await instance.fetch(
            make_content(), ProviderContext(canonical_key="fc2_ppv:1234567")
        )
    finally:
        await client.aclose()

    assert result is not None
    assert result.title_ja


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


# -- FC2 description: the article page only carries a truncated og:description ---


def test_fc2_marks_its_description_as_a_summary() -> None:
    """The official page only publishes a teaser; the card layer shows it as is."""

    result = provider(Fc2Provider).parse(
        page("fc2.html", "https://adult.contents.fc2.com/article/1234567/"), make_content()
    )

    assert result is not None
    assert result.description_quality == "summary"


def test_fc2_falls_back_to_the_meta_description() -> None:
    """No widget → meta[name=description], which is longer than og:description."""

    result = provider(Fc2Provider).parse(
        page("fc2_article_with_description.html",
             "https://adult.contents.fc2.com/article/1234567/"),
        make_content(),
    )

    assert result is not None
    assert result.raw["description_source"] == "meta"
    assert result.description_ja == "サンプル作品の説明 その二 その三"


def test_fc2_falls_back_to_og_description_last() -> None:
    html = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        '<meta property="og:description" content="短い概要…">'
        "</head><body></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(url="https://adult.contents.fc2.com/article/1234567/", html=html, status=200),
        make_content(),
    )

    assert result is not None
    assert result.raw["description_source"] == "og"
    assert result.description_quality == "summary"
    assert result.description_ja == "短い概要…"


def test_fc2_without_any_description_is_empty_not_fabricated() -> None:
    html = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        "</head><body></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(url="https://adult.contents.fc2.com/article/1234567/", html=html, status=200),
        make_content(),
    )

    assert result is not None
    assert result.description_ja is None
    assert result.description_quality is None
    assert result.raw["description_source"] is None


# -- HTML entities: decoded exactly once, then re-escaped by the card layer -----


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("&quot;x&quot;", '"x"'),
        ("a &amp; b", "a & b"),
        ("&lt;b&gt;粗&lt;/b&gt;", "<b>粗</b>"),
        ("it&#39;s", "it's"),
        ("a&nbsp;b", "a b"),
        ("前&nbsp;&nbsp;後", "前 後"),
        # One pass only: a double-encoded entity stays single-encoded.
        ("&amp;quot;", "&quot;"),
        ("literal &amp;amp;", "literal &amp;"),
        ("  a \n\t b  ", "a b"),
    ],
)
def test_clean_text_decodes_entities_once(raw: str, expected: str) -> None:
    from app.providers.base import clean_text

    assert clean_text(raw) == expected


def test_provider_description_entities_reach_the_card_decoded() -> None:
    """The reported bug: ``&amp;quot;`` on the card, twice-escaped."""

    from app.services.cards import CardSource, build_card_messages

    html_page = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        '<meta name="description" content="タイトルは&quot;衝撃&quot; &amp; 話題 &#39;No.1&#39;">'
        "</head><body></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(
            url="https://adult.contents.fc2.com/article/1234567/", html=html_page, status=200
        ),
        make_content(),
    )
    assert result is not None
    assert result.description_ja == "タイトルは\"衝撃\" & 話題 'No.1'"

    messages = build_card_messages(
        CardSource(
            content_id="0b6f0d4e-0000-4000-8000-000000000001",
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
            title=result.title_ja,
            description_ja=result.description_ja,
            description_zh=None,
            censorship=Censorship.UNKNOWN,
            nyaa_url="https://sukebei.nyaa.si/view/1",
            official_url=None,
        )
    )
    caption = messages.caption
    assert "&amp;quot;" not in caption
    assert "&quot;" not in caption
    assert '"衝撃"' in caption
    # A real ampersand is escaped once, for parse_mode=HTML.
    assert "&amp; 話題" in caption
    assert "&#39;" not in caption


def test_title_entities_are_decoded_before_matching() -> None:
    html_page = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 &quot;衝撃&quot; 作品">'
        "</head><body></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(
            url="https://adult.contents.fc2.com/article/1234567/", html=html_page, status=200
        ),
        make_content(),
    )
    assert result is not None
    assert result.title_ja == '"衝撃" 作品'


# -- censorship tag: short forms, title evidence, conflicts --------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("【無】素人作品", Censorship.UNCENSORED),
        ("【无】素人作品", Censorship.UNCENSORED),
        ("無修正 作品", Censorship.UNCENSORED),
        ("无修正 作品", Censorship.UNCENSORED),
        ("無碼 作品", Censorship.UNCENSORED),
        ("UNCENSORED 作品", Censorship.UNCENSORED),
        ("【有】素人作品", Censorship.CENSORED),
        ("有修正 作品", Censorship.CENSORED),
        ("修正あり 作品", Censorship.CENSORED),
        ("Censored 作品", Censorship.CENSORED),
        ("有碼 作品", Censorship.CENSORED),
        ("【薄】素人作品", Censorship.THIN),
        ("薄修正 作品", Censorship.THIN),
        # Mosaic is censored, never thin when it is the only evidence.
        ("モザイク処理 作品", Censorship.CENSORED),
        ("素人作品", None),
    ],
)
def test_censorship_vocabulary(text: str, expected: Censorship | None) -> None:
    from app.providers.base import infer_censorship

    verdict, evidence = infer_censorship({"title": text})
    assert verdict is expected
    if expected is None:
        assert evidence["matches"] == []
    else:
        assert evidence["matches"]


def test_censorship_normalises_fullwidth_and_case() -> None:
    from app.providers.base import infer_censorship

    verdict, _ = infer_censorship({"title": "ＵＮＣＥＮＳＯＲＥＤ　作品"})
    assert verdict is Censorship.UNCENSORED


def test_censorship_reads_the_release_title() -> None:
    """The tag usually lives in the Nyaa title, not on the FC2 page."""

    html_page = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        '<meta name="description" content="説明文です。">'
        "</head><body><p>サンプル</p></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(
            url="https://adult.contents.fc2.com/article/1234567/",
            html=html_page,
            status=200,
            release_title="FC2-PPV-1234567 【無】作品",
        ),
        make_content(),
    )

    assert result is not None
    assert result.censorship is Censorship.UNCENSORED
    evidence = result.raw["censorship_evidence"]
    assert evidence["matches"][0]["field"] == "release_title"
    assert evidence["matches"][0]["token"] == "【無】"


def test_censorship_conflict_becomes_unknown_with_evidence() -> None:
    html_page = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 【無】作品">'
        '<meta name="description" content="説明文です。">'
        "</head><body><p>この作品は有修正です。</p></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(
            url="https://adult.contents.fc2.com/article/1234567/", html=html_page, status=200
        ),
        make_content(),
    )

    assert result is not None
    assert result.censorship is Censorship.UNKNOWN
    evidence = result.raw["censorship_evidence"]
    assert evidence["conflict"] is True
    fields = {item["field"] for item in evidence["matches"]}
    assert {"title", "page"} <= fields


def test_censorship_evidence_is_recorded_even_when_it_misses() -> None:
    html_page = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 作品">'
        "</head><body></body></html>"
    )
    result = provider(Fc2Provider).parse(
        ProviderPage(
            url="https://adult.contents.fc2.com/article/1234567/", html=html_page, status=200
        ),
        make_content(),
    )

    assert result is not None
    assert result.censorship is None
    assert result.raw["censorship_evidence"] == {"matches": [], "conflict": False}


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Prefix inside the bracket, not the whole bracket.
        ("【無・ごっくん＆中出し】素人", Censorship.UNCENSORED),
        ("【無】素人", Censorship.UNCENSORED),
        ("【無修正】素人", Censorship.UNCENSORED),
        ("【無 素人】", Censorship.UNCENSORED),
        ("【無-素人】", Censorship.UNCENSORED),
        ("【有・素人】", Censorship.CENSORED),
        ("【有】素人", Censorship.CENSORED),
        ("【薄・素人】", Censorship.THIN),
        # Not about censoring: never guessed.
        ("【無料】素人", None),
        ("【無制限】素人", None),
        ("【無断】素人", None),
        ("【有料】素人", None),
        # A bracketed non-tag must not mask a real tag elsewhere.
        ("【無料】だが無修正", Censorship.UNCENSORED),
    ],
)
def test_censorship_bracket_prefixes(text: str, expected: Censorship | None) -> None:
    from app.providers.base import infer_censorship

    verdict, evidence = infer_censorship({"title": text})
    assert verdict is expected
    if expected is not None:
        assert evidence["matches"]


# -- Nyaa fallback: the safety net when the official site is gone --------------


def test_nyaa_fallback_rewrites_a_torrent_download_url() -> None:
    """RSS links are ``/download/<id>.torrent``; fetching that returns bencode."""

    fallback = provider(NyaaFallbackProvider)
    url = fallback.url_for_release(
        "https://sukebei.nyaa.si/download/4761220.torrent", "4761220"
    )
    assert url == "https://sukebei.nyaa.si/view/4761220"


def test_nyaa_fallback_uses_the_release_title_when_the_page_has_none() -> None:
    fallback = provider(NyaaFallbackProvider)
    html = "<html><head></head><body><p>removed</p></body></html>"
    result = fallback.parse(
        ProviderPage(
            url="https://sukebei.nyaa.si/view/4761220",
            html=html,
            status=200,
            release_title="FC2-PPV-1234567 上传者给的标题",
        ),
        make_content(),
    )

    assert result is not None
    assert result.title_ja == "FC2-PPV-1234567 上传者给的标题"
    assert result.description_ja is None


def test_nyaa_fallback_strips_the_site_suffix_from_og_title() -> None:
    fallback = provider(NyaaFallbackProvider)
    html = (
        '<html><head><meta property="og:title" content="FC2-PPV-1234567 タイトル :: Sukebei">'
        "</head><body></body></html>"
    )
    result = fallback.parse(
        ProviderPage(url="https://sukebei.nyaa.si/view/4761220", html=html, status=200),
        make_content(),
    )
    assert result is not None
    assert result.title_ja == "FC2-PPV-1234567 タイトル"


def test_nyaa_fallback_extracts_bare_image_urls_from_the_description() -> None:
    fallback = provider(NyaaFallbackProvider)
    html = (
        "<html><body><h3 class='panel-title'>FC2-PPV-1234567 タイトル</h3>"
        "<div class='panel-body'><p>サンプル https://img.example.com/a.jpg です</p>"
        "<p>https://img.example.com/b.png</p></div></body></html>"
    )
    result = fallback.parse(
        ProviderPage(url="https://sukebei.nyaa.si/view/4761220", html=html, status=200),
        make_content(),
    )

    assert result is not None
    assert result.cover is not None and result.cover.url == "https://img.example.com/a.jpg"
    assert [item.url for item in result.previews] == ["https://img.example.com/b.png"]


def test_nyaa_fallback_ignores_the_default_avatar_as_cover() -> None:
    fallback = provider(NyaaFallbackProvider)
    html = (
        '<html><head><meta property="og:image" content="/static/img/avatar/default.png">'
        "</head><body><h3 class='panel-title'>FC2-PPV-1234567 タイトル</h3></body></html>"
    )
    result = fallback.parse(
        ProviderPage(url="https://sukebei.nyaa.si/view/4761220", html=html, status=200),
        make_content(),
    )

    assert result is not None
    assert result.cover is None
    assert result.previews == []
