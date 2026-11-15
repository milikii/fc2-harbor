"""Nyaa parsers — spec 6.2 / 7.4 / 22.2.

Fixtures are constructed samples (see tests/fixtures/nyaa/README.md): they mirror
the structure of the public pages, never a captured real payload.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.sources.nyaa import (
    NyaaParseError,
    NyaaRssParser,
    NyaaSearchParser,
    build_magnet,
    item_id_from_url,
    parse_http_date,
    split_uploader,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nyaa"


def load(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def test_rss_parses_documented_fields() -> None:
    items = NyaaRssParser().parse(load("rss_fc2.xml"))

    assert [item.item_id for item in items] == [
        "1900001",
        "1900002",
        "1900003",
        "1900004",
    ]
    first = items[0]
    assert first.title == "FC2-PPV-1234567 Sample Title"
    assert first.uploader == "Uploader A"
    assert first.info_hash == "0123456789abcdef0123456789abcdef01234567"
    assert first.pub_date == datetime(2020, 11, 14, 10, 0, tzinfo=UTC)
    assert first.seeders == 12
    assert first.size_text == "1.2 GiB"
    assert first.category == "Art - Anime"
    assert first.link == "https://sukebei.nyaa.si/view/1900001"
    assert "sample description" in first.raw["description"]


def test_rss_item_without_hash_is_kept_for_later_filtering() -> None:
    items = NyaaRssParser().parse(load("rss_fc2.xml"))
    assert items[3].info_hash is None
    assert items[3].title == "FC2-PPV-3334444 No Hash Item"


def test_rss_missing_fields_do_not_crash() -> None:
    items = NyaaRssParser().parse(load("rss_missing_fields.xml"))
    assert len(items) == 1
    item = items[0]
    assert item.uploader is None
    assert item.title == "FC2PPV9998887 Bare Title Without Uploader"
    assert item.pub_date is None
    assert item.info_hash is None
    assert item.size_text is None


def test_rss_without_channel_raises() -> None:
    with pytest.raises(NyaaParseError):
        NyaaRssParser().parse(b"<rss><foo></foo></rss>")


def test_invalid_xml_raises() -> None:
    with pytest.raises(NyaaParseError):
        NyaaRssParser().parse(b"<rss><channel>")


def test_search_page_parses_rows_and_magnet() -> None:
    items = NyaaSearchParser().parse(load("search_page1.html").decode(), "https://sukebei.nyaa.si")

    assert len(items) == 2
    first = items[0]
    assert first.item_id == "1900001"
    assert first.title == "FC2-PPV-1234567 Sample Title"
    assert first.uploader == "Uploader A"
    assert first.info_hash == "0123456789abcdef0123456789abcdef01234567"
    assert first.magnet is not None and first.magnet.startswith("magnet:?xt=urn:btih:")
    assert first.size_text == "1.2 GiB"
    assert first.seeders == 12
    assert first.category == "Art - Anime"
    assert first.pub_date == datetime(2020, 11, 14, 10, 0, tzinfo=UTC)

    # Second row has no magnet link; the hash stays unknown here.
    assert items[1].magnet is None
    assert items[1].info_hash is None


def test_search_page_with_empty_table_returns_nothing() -> None:
    assert (
        NyaaSearchParser().parse(load("search_empty.html").decode(), "https://sukebei.nyaa.si")
        == []
    )


def test_error_page_raises_instead_of_looking_empty() -> None:
    """A challenge page must never be mistaken for "no results"."""

    with pytest.raises(NyaaParseError):
        NyaaSearchParser().parse(load("error_page.html").decode(), "https://sukebei.nyaa.si")


def test_split_uploader() -> None:
    assert split_uploader("[Uploader] Title") == ("Uploader", "Title")
    assert split_uploader("Plain Title") == (None, "Plain Title")
    assert split_uploader("[not closed Title") == (None, "[not closed Title")


def test_item_id_from_url() -> None:
    assert item_id_from_url("https://sukebei.nyaa.si/view/1234567") == "1234567"
    assert item_id_from_url("https://sukebei.nyaa.si/view/1234567/") == "1234567"
    assert item_id_from_url(None) is None
    assert item_id_from_url("https://sukebei.nyaa.si/") is None


@pytest.mark.parametrize(
    "link",
    [
        "https://sukebei.nyaa.si/view/4761220",
        "https://sukebei.nyaa.si/view/4761220/",
        "https://sukebei.nyaa.si/download/4761220.torrent",
        "https://sukebei.nyaa.si/download/4761220.torrent?foo=bar",
    ],
)
def test_view_and_download_links_share_one_item_id(link: str) -> None:
    """Regression: RSS uses /download/<id>.torrent, the search page /view/<id>.

    Both must yield the same ``source_item_id``, otherwise one torrent produces
    two release rows and the (source, source_item_id) unique constraint misses it.
    """

    assert item_id_from_url(link) == "4761220"


def test_rss_and_search_parse_the_same_torrent_to_one_id() -> None:
    rss_item = NyaaRssParser().parse(
        b'<?xml version="1.0"?><rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa">'
        b"<channel><title>t</title><item>"
        b"<title>FC2-PPV-4761220 Same Torrent</title>"
        b"<link>https://sukebei.nyaa.si/download/4761220.torrent</link>"
        b"<nyaa:infoHash>cccccccccccccccccccccccccccccccccccccccc</nyaa:infoHash>"
        b"</item></channel></rss>"
    )[0]
    search_item = NyaaSearchParser().parse(
        (FIXTURES / "search_page_realistic.html").read_text(),
        "https://sukebei.nyaa.si",
    )[0]

    assert rss_item.item_id == search_item.item_id == "4761220"
    assert rss_item.info_hash == search_item.info_hash


def test_realistic_search_markup_still_yields_size_and_seeders() -> None:
    """The real page has no td-size / td-green classes; offsets must work."""

    items = NyaaSearchParser().parse(
        (FIXTURES / "search_page_realistic.html").read_text(),
        "https://sukebei.nyaa.si",
    )

    assert [item.item_id for item in items] == ["4761220", "4761221"]
    assert items[0].size_text == "3.4 GiB"
    assert items[0].seeders == 27
    assert items[0].pub_date == datetime(2020, 11, 14, 10, 0, tzinfo=UTC)
    assert items[1].size_text == "900.0 MiB"
    assert items[1].seeders == 5


def test_parse_http_date() -> None:
    assert parse_http_date("Sat, 14 Nov 2020 10:00:00 -0000") == datetime(
        2020, 11, 14, 10, 0, tzinfo=UTC
    )
    assert parse_http_date("2020-11-14T10:00:00Z") == datetime(2020, 11, 14, 10, 0, tzinfo=UTC)
    assert parse_http_date("not a date") is None
    assert parse_http_date(None) is None


def test_build_magnet_encodes_title_and_trackers() -> None:
    magnet = build_magnet(
        "0123456789ABCDEF0123456789ABCDEF01234567",
        "FC2-PPV-1234567 タイトル",
        ["udp://tracker.example:6969/announce"],
    )
    assert magnet.startswith("magnet:?xt=urn:btih:0123456789abcdef")
    assert "dn=FC2-PPV-1234567%20" in magnet
    assert "tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce" in magnet
