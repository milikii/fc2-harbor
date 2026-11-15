"""Nyaa discovery over a mocked transport — spec 6.2 / 22.2.

No real request is made: every response comes from respx or a static fixture.
"""

from __future__ import annotations

import itertools
import re
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
import respx
from app.config import Settings
from app.network.clients import HttpError
from app.sources.nyaa import NyaaCursor, NyaaSource

from tests.support import make_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "nyaa"
RSS_FIXTURE = (FIXTURES / "rss_fc2.xml").read_bytes()
_PUBDATE_RE = re.compile(r"<pubDate>[^<]*</pubDate>")


def rss_fixture() -> bytes:
    """The static RSS fixture with its pubDates moved to "recently".

    The file carries absolute dates, so a wall-clock lookback window silently drops
    every item once the fixture is older than ``initial_lookback_hours``.
    """

    counter = itertools.count()
    def replace(_match: re.Match[str]) -> str:
        stamp = datetime.now(UTC) - timedelta(minutes=5 * next(counter) + 5)
        return f"<pubDate>{format_datetime(stamp)}</pubDate>"

    return _PUBDATE_RE.sub(replace, RSS_FIXTURE.decode("utf-8")).encode("utf-8")
SEARCH_FIXTURE = (FIXTURES / "search_page1.html").read_bytes()
ERROR_FIXTURE = (FIXTURES / "error_page.html").read_bytes()


def rss_url(settings: Settings) -> str:
    return settings.nyaa.base_url.rstrip("/") + settings.nyaa.rss_path


def search_url(settings: Settings, page: int) -> str:
    return settings.nyaa.base_url.rstrip("/") + settings.nyaa.search_path.format(page=page)


def rss_with_items(count: int, *, start_id: int = 1910000) -> bytes:
    items = []
    for index in range(count):
        item_id = start_id + index
        items.append(
            f"""
<item>
  <title>[Uploader {index}] FC2-PPV-{1000000 + index} Item {index}</title>
  <link>https://sukebei.nyaa.si/view/{item_id}</link>
  <guid isPermaLink="true">https://sukebei.nyaa.si/view/{item_id}</guid>
  <pubDate>Sat, 14 Nov 2026 10:00:00 -0000</pubDate>
  <nyaa:seeders>1</nyaa:seeders>
  <nyaa:infoHash>{index:040x}</nyaa:infoHash>
  <nyaa:size>1.0 GiB</nyaa:size>
  <nyaa:category>Art - Anime</nyaa:category>
</item>"""
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>'
        "<title>Sukebei</title><link>https://sukebei.nyaa.si/</link>"
        + "".join(items)
        + "</channel></rss>"
    ).encode()


@respx.mock(assert_all_called=False)
async def test_discover_maps_fc2_items_and_filters_the_rest(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, nyaa={"trackers": ["udp://tracker.example:6969/announce"]})
    respx_mock.get(rss_url(settings)).respond(200, content=rss_fixture())

    source = NyaaSource(settings=settings)
    try:
        candidates = await source.discover(None)
    finally:
        await source.aclose()

    assert [candidate.source_item_id for candidate in candidates] == ["1900001", "1900002"]
    first = candidates[0]
    assert first.source == "nyaa"
    assert first.content_hint is not None
    assert first.content_hint.canonical_key == "fc2_ppv:1234567"
    assert first.content_hint.display_code == "FC2-PPV-1234567"
    assert first.info_hash == "0123456789abcdef0123456789abcdef01234567"
    assert first.magnet is not None
    assert first.magnet.startswith("magnet:?xt=urn:btih:0123456789abcdef")
    assert "tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce" in first.magnet
    assert first.published_at is not None
    assert first.raw_payload["uploader"] == "Uploader A"

    # The non-FC2 item and the item without a hash are filtered with a reason.
    assert source.last_stats.filtered["1900003"] == "not_fc2"
    assert source.last_stats.filtered["1900004"] == "no_magnet"
    assert source.last_stats.rss_items == 4


@respx.mock(assert_all_called=False)
async def test_etag_is_sent_and_304_returns_no_candidates(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path)
    route = respx_mock.get(rss_url(settings)).mock(
        side_effect=[
            httpx.Response(200, content=rss_fixture(), headers={"ETag": '"v1"'}),
            httpx.Response(304),
        ]
    )

    source = NyaaSource(settings=settings)
    try:
        first = await source.discover(None)
        assert len(first) == 2
        cursor = source.last_cursor
        assert cursor is not None and cursor.etag == '"v1"'

        second = await source.discover(cursor.to_dict())
        assert second == []
        assert source.last_stats.not_modified is True
    finally:
        await source.aclose()

    assert route.calls[1].request.headers["If-None-Match"] == '"v1"'


@respx.mock(assert_all_called=False)
async def test_first_run_applies_lookback_window(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, nyaa={"initial_lookback_hours": 1})
    payload = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:nyaa="https://nyaa.si/xmlns/nyaa"><channel>'
        "<title>Sukebei</title>"
        "<item><title>FC2-PPV-1111111 old</title>"
        "<link>https://sukebei.nyaa.si/view/1</link>"
        "<pubDate>Thu, 01 Jan 1970 00:00:00 -0000</pubDate>"
        "<nyaa:infoHash>1111111111111111111111111111111111111111</nyaa:infoHash></item>"
        "<item><title>FC2-PPV-2222222 recent</title>"
        "<link>https://sukebei.nyaa.si/view/2</link>"
        f"<pubDate>{format_datetime(datetime.now(UTC) - timedelta(minutes=10))}</pubDate>"
        "<nyaa:infoHash>2222222222222222222222222222222222222222</nyaa:infoHash></item>"
        "</channel></rss>"
    ).encode()
    respx_mock.get(rss_url(settings)).respond(200, content=payload)

    source = NyaaSource(settings=settings)
    try:
        first = await source.discover(None)
        assert [candidate.source_item_id for candidate in first] == ["2"]
        assert source.last_stats.filtered["1"] == "older_than_lookback"

        # With a cursor the lookback window no longer applies: the whole page is
        # re-checked and the database decides what is new (overlap re-check).
        again = await source.discover(source.last_cursor.to_dict())
        assert [candidate.source_item_id for candidate in again] == ["1", "2"]
    finally:
        await source.aclose()


@respx.mock(assert_all_called=False)
async def test_full_rss_window_triggers_search_pagination(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, nyaa={"rss_page_size": 10, "search_max_pages": 3})
    respx_mock.get(rss_url(settings)).respond(200, content=rss_with_items(10))
    respx_mock.get(search_url(settings, 2)).respond(200, content=SEARCH_FIXTURE)

    source = NyaaSource(settings=settings)
    try:
        candidates = await source.discover(None)
    finally:
        await source.aclose()

    ids = [candidate.source_item_id for candidate in candidates]
    # 10 RSS items + the search row that carries a magnet. The second search row
    # has no magnet link, so it is filtered (no_magnet) instead of inventing one.
    assert len(ids) == 11
    assert "1900001" in ids
    assert "1900002" not in ids
    assert source.last_stats.pages_fetched == 2


@respx.mock(assert_all_called=False)
async def test_pagination_failure_keeps_the_rss_round(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(
        tmp_path, nyaa={"rss_page_size": 10, "search_max_pages": 3, "retries": 0}
    )
    respx_mock.get(rss_url(settings)).respond(200, content=rss_with_items(10))
    respx_mock.get(search_url(settings, 2)).respond(500)

    source = NyaaSource(settings=settings)
    try:
        candidates = await source.discover(None)
    finally:
        await source.aclose()

    assert len(candidates) == 10
    assert source.last_stats.pages_fetched == 2


@respx.mock(assert_all_called=False)
async def test_rss_failure_propagates(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, nyaa={"retries": 0})
    respx_mock.get(rss_url(settings)).respond(503)

    source = NyaaSource(settings=settings)
    try:
        with pytest.raises(HttpError):
            await source.discover(None)
    finally:
        await source.aclose()


@respx.mock(assert_all_called=False)
async def test_broken_search_page_does_not_produce_fake_candidates(
    respx_mock: respx.MockRouter, tmp_path: Path
) -> None:
    settings = make_settings(tmp_path, nyaa={"rss_page_size": 10, "search_max_pages": 2})
    respx_mock.get(rss_url(settings)).respond(200, content=rss_with_items(10))
    respx_mock.get(search_url(settings, 2)).respond(200, content=ERROR_FIXTURE)

    source = NyaaSource(settings=settings)
    try:
        candidates = await source.discover(None)
    finally:
        await source.aclose()

    # The challenge page has no torrent table: the parser raises, the round keeps
    # only the ten genuine RSS candidates.
    assert len(candidates) == 10


@respx.mock(assert_all_called=False)
async def test_user_agent_is_sent(respx_mock: respx.MockRouter, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, nyaa={"user_agent": "fc2-harbor-test/9.9"})
    route = respx_mock.get(rss_url(settings)).respond(200, content=rss_fixture())

    source = NyaaSource(settings=settings)
    try:
        await source.discover(None)
    finally:
        await source.aclose()

    assert route.calls[0].request.headers["User-Agent"] == "fc2-harbor-test/9.9"


def test_cursor_roundtrip_and_bound() -> None:
    cursor = NyaaCursor(etag='"v1"', last_modified="Sat, 14 Nov 2026 10:00:00 GMT")
    assert NyaaCursor.from_dict(cursor.to_dict()) == cursor
    assert NyaaCursor.from_dict(None) == NyaaCursor()
    assert NyaaCursor.from_dict({"seen_item_ids": ["a", "b"]}).seen_item_ids == ("a", "b")
