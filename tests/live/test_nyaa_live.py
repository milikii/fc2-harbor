"""Manual live smoke test against the real site — spec 22.2.

Never runs in CI: it is skipped unless ``pytest --run-live`` is passed, and it
makes exactly two requests (RSS + one search page). A site outage is reported as
a skip, not a failure, so the smoke run does not cry wolf.

What it proves: the adapter can still find the torrent table / RSS channel and
normalise at least one FC2 item against the live markup. It does not download
anything.
"""

from __future__ import annotations

import os

import pytest
from app.config import Settings
from app.network.clients import HttpError
from app.sources.nyaa import NyaaParseError, NyaaSearchParser

pytestmark = pytest.mark.live

#: This machine (and the NAS behind a filtered uplink) needs an explicit proxy;
#: the client is trust_env=False by design, so the smoke run passes it in.
LIVE_PROXY_URL = os.environ.get("LIVE_PROXY_URL", "").strip()


def require_proxy() -> None:
    if not LIVE_PROXY_URL:
        pytest.skip("set LIVE_PROXY_URL (e.g. http://192.168.1.220:7890) to run live tests")


def live_settings() -> Settings:
    # Minimal in-memory configuration: no database, no tokens.
    config: dict[str, object] = {
        "telegram": {"manual_bot_token": "123456:TEST", "allowed_user_ids": [1]},
        "provider": {
            "priorities": {
                "jav_fc2": [
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
            },
            "sources": {
                "Fc2": {"host": "adult.contents.fc2.com", "proxy": "direct"},
                "fc2_hub": {"host": "javten.com", "proxy": "direct"},
                "Javdb": {"host": "javdb.com", "proxy": "direct"},
                "Mmtv": {"host": "7mmtv.sx", "proxy": "direct"},
                "freejavbt": {"host": "freejavbt.com", "proxy": "direct"},
                "miss_av": {"host": "missav123.com", "proxy": "direct"},
                "Javlibrary": {"host": "javlibrary.com", "proxy": "direct"},
                "avsox": {"host": "avsox.click", "proxy": "direct"},
                "Madou": {"host": "madou.club", "proxy": "direct"},
            },
        },
        "proxy_profiles": {
            "direct": {"type": "direct"},
            **({"live": {"type": "http", "url": LIVE_PROXY_URL}} if LIVE_PROXY_URL else {}),
        },
        "downloaders": {
            "qbittorrent": {
                "enabled": True,
                "url": "http://127.0.0.1:8080",
                "username": "admin",
                "password": "x",
            }
        },
        "storage": {
            "path_mappings": [{"remote": "/downloads", "local": "/downloads"}],
            "final_roots": {
                "fc2": "/library/fc2",
                "jav": "/library/jav",
                "cn": "/library/cn",
                "western": "/library/western",
                "review": "/library/_review",
            },
        },
    }
    if LIVE_PROXY_URL:
        config["nyaa"] = {"proxy": "live"}  # type: ignore[assignment]
    return Settings.from_mapping(config, env={})


async def test_live_rss_and_search_still_parse() -> None:
    require_proxy()
    from app.sources.nyaa import NyaaSource

    settings = live_settings()
    source = NyaaSource(settings=settings)
    try:
        try:
            candidates = await source.discover(None)
        except HttpError as exc:
            pytest.skip(f"sukebei.nyaa.si unreachable: {exc}")
        assert source.last_stats.rss_items > 0, "RSS returned no items"
        # Not every recent item is FC2, but the parser must classify them all.
        assert source.last_stats.rss_items >= len(candidates)
        for candidate in candidates:
            assert candidate.content_hint is not None
            assert candidate.content_hint.canonical_key.startswith("fc2_ppv:")
    finally:
        await source.aclose()


async def test_live_search_page_still_has_the_torrent_table() -> None:
    require_proxy()
    from app.network.clients import HttpClient

    settings = live_settings()
    client = HttpClient(user_agent=settings.nyaa.user_agent, retries=1)
    proxy_url = settings.proxy_profiles["live"].url if LIVE_PROXY_URL else None
    try:
        url = settings.nyaa.base_url.rstrip("/") + settings.nyaa.search_path.format(page=1)
        try:
            response = await client.get(url, proxy_url=proxy_url)
        except HttpError as exc:
            pytest.skip(f"sukebei.nyaa.si unreachable: {exc}")
        try:
            items = NyaaSearchParser().parse(response.text, settings.nyaa.base_url)
        except NyaaParseError as exc:
            pytest.fail(f"search page structure changed: {exc}")
        assert items, "search page parsed zero rows"
    finally:
        await client.aclose()
