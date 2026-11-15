"""Browser-like headers and provider pacing — anti-bot defence.

These tests only prove what we send, not that a site accepts it: Cloudflare answers
with a JS challenge regardless of headers, which is a separate decision.
"""

from __future__ import annotations

import httpx
import pytest
from app.network.browser_headers import CHROME_USER_AGENT, provider_headers
from app.network.clients import HttpClient
from app.providers.base import BaseProvider, ProviderError, ProviderPage
from app.schemas.metadata import MetadataResult, ProviderContext


def test_headers_look_like_a_chrome_navigation() -> None:
    headers = provider_headers("example.com")

    assert headers["User-Agent"] == CHROME_USER_AGENT
    assert "Chrome/" in headers["User-Agent"]
    assert headers["Accept"].startswith("text/html")
    assert "ja" in headers["Accept-Language"]
    assert headers["Upgrade-Insecure-Requests"] == "1"
    assert headers["Sec-Fetch-Mode"] == "navigate"
    assert headers["Sec-Fetch-Dest"] == "document"
    assert headers["Sec-Fetch-Site"] == "none"  # typed URL, no referer
    assert "Referer" not in headers


def test_headers_derive_sec_fetch_site_from_the_referer() -> None:
    same = provider_headers("example.com", referer="https://example.com/list")
    cross = provider_headers("example.com", referer="https://other.example/x")

    assert same["Sec-Fetch-Site"] == "same-origin"
    assert same["Referer"] == "https://example.com/list"
    assert cross["Sec-Fetch-Site"] == "cross-site"


def test_configuration_overrides_win() -> None:
    headers = provider_headers(
        "example.com", overrides={"Accept-Language": "zh-CN,zh;q=0.9"}
    )
    assert headers["Accept-Language"] == "zh-CN,zh;q=0.9"


class RecordingProvider(BaseProvider):
    id = "Recorder"
    host = "example.com"
    proxy_key = "Recorder"

    def build_url(self, content: object) -> str:
        return "https://example.com/search?q=1"

    def parse(self, page: ProviderPage, content: object) -> MetadataResult | None:
        return None


async def test_fetch_warms_up_first_then_sends_browser_headers() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if str(request.url) == "https://example.com/":
            return httpx.Response(200, text="home", headers={"set-cookie": "sid=abc; Path=/"})
        return httpx.Response(200, text="<html></html>")

    client = HttpClient(transport=httpx.MockTransport(handler))
    provider = RecordingProvider(
        client=client, proxy_url=None, warmup_url="https://example.com/"
    )
    content = object()

    with pytest.raises(ProviderError):  # parse returns None -> not_found
        await provider.fetch(content, ProviderContext(canonical_key="fc2_ppv:1"))

    assert [str(r.url) for r in requests] == [
        "https://example.com/",
        "https://example.com/search?q=1",
    ]
    search = requests[1]
    assert search.headers["Referer"] == "https://example.com/"
    assert search.headers["Sec-Fetch-Site"] == "same-origin"
    assert "Chrome/" in search.headers["User-Agent"]
    # The cookie from the warm-up page travels with the real request.
    assert "sid=abc" in search.headers.get("cookie", "")
    await client.aclose()


async def test_warmup_runs_only_once() -> None:
    hits: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(str(request.url))
        return httpx.Response(200, text="<html></html>")

    client = HttpClient(transport=httpx.MockTransport(handler))
    provider = RecordingProvider(
        client=client, proxy_url=None, warmup_url="https://example.com/"
    )

    for _ in range(2):
        with pytest.raises(ProviderError):
            await provider.fetch(object(), ProviderContext(canonical_key="fc2_ppv:1"))

    assert hits.count("https://example.com/") == 1
    await client.aclose()


async def test_warmup_failure_does_not_stop_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/":
            return httpx.Response(500)
        return httpx.Response(200, text="<html></html>")

    client = HttpClient(transport=httpx.MockTransport(handler))
    provider = RecordingProvider(
        client=client, proxy_url=None, warmup_url="https://example.com/"
    )

    with pytest.raises(ProviderError):  # still reaches the search page
        await provider.fetch(object(), ProviderContext(canonical_key="fc2_ppv:1"))
    await client.aclose()


async def test_throttle_sleeps_between_requests() -> None:
    import time

    client = HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="")))
    provider = RecordingProvider(client=client, proxy_url=None, min_interval_seconds=0.05)

    started = time.monotonic()
    await provider._throttle()
    await provider._throttle()
    elapsed = time.monotonic() - started

    assert elapsed >= 0.05
    await client.aclose()
