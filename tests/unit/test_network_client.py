"""HTTP client policy — spec 6.2 / 8.3.

Only idempotent GETs, bounded retries, Retry-After honoured, per-domain
concurrency capped, conditional requests supported. No real network.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx
from app.network.clients import (
    DomainLimiter,
    HttpClient,
    HttpError,
    HttpNetworkError,
    parse_retry_after,
)

URL = "https://sukebei.nyaa.si/?page=rss"


def make_client(**kwargs: object) -> tuple[HttpClient, list[float]]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    options: dict[str, object] = {
        "user_agent": "test-agent/1.0",
        "retries": 2,
        "per_domain_concurrency": 2,
        "max_retry_after_seconds": 30,
        "sleeper": fake_sleep,
    }
    options.update(kwargs)
    return HttpClient(**options), delays  # type: ignore[arg-type]


def test_parse_retry_after_variants() -> None:
    assert parse_retry_after("3", cap_seconds=60) == 3.0
    assert parse_retry_after("600", cap_seconds=30) == 30.0
    assert parse_retry_after("-5", cap_seconds=30) == 0.0
    assert parse_retry_after(None, cap_seconds=30) is None
    assert parse_retry_after("not-a-number", cap_seconds=30) is None

    future = datetime.now(UTC) + timedelta(seconds=20)
    parsed = parse_retry_after(format_datetime(future), cap_seconds=60)
    assert parsed is not None and 10 <= parsed <= 21


@respx.mock(assert_all_called=False)
async def test_get_returns_body_and_sends_user_agent(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(200, content=b"<rss/>", headers={"ETag": '"v1"'})
    client, _ = make_client()

    try:
        response = await client.get(URL)
    finally:
        await client.aclose()

    assert response.status == 200
    assert response.content == b"<rss/>"
    assert response.header("etag") == '"v1"'
    assert route.calls[0].request.headers["User-Agent"] == "test-agent/1.0"


@respx.mock(assert_all_called=False)
async def test_304_is_returned_without_body(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).respond(304)
    client, _ = make_client()
    try:
        response = await client.get(URL, headers={"If-None-Match": '"v1"'})
    finally:
        await client.aclose()

    assert response.not_modified is True
    assert response.content == b""


@respx.mock(assert_all_called=False)
async def test_retry_after_is_honoured(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=b"ok"),
        ]
    )
    client, delays = make_client()
    try:
        response = await client.get(URL)
    finally:
        await client.aclose()

    assert response.status == 200
    assert delays == [7.0]


@respx.mock(assert_all_called=False)
async def test_retries_are_bounded_and_then_raise(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(503)
    client, _ = make_client(retries=1)
    try:
        with pytest.raises(HttpError) as excinfo:
            await client.get(URL)
    finally:
        await client.aclose()

    assert excinfo.value.status == 503
    assert route.call_count == 2  # initial attempt + 1 retry


@respx.mock(assert_all_called=False)
async def test_client_errors_are_not_retried(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.get(URL).respond(404)
    client, delays = make_client()
    try:
        with pytest.raises(HttpError) as excinfo:
            await client.get(URL)
    finally:
        await client.aclose()

    assert excinfo.value.status == 404
    assert route.call_count == 1
    assert delays == []


@respx.mock(assert_all_called=False)
async def test_network_error_is_retried_then_wrapped(respx_mock: respx.MockRouter) -> None:
    respx_mock.get(URL).mock(side_effect=httpx.ConnectError("boom"))
    client, delays = make_client(retries=2)
    try:
        with pytest.raises(HttpNetworkError):
            await client.get(URL)
    finally:
        await client.aclose()

    assert len(delays) == 2  # two sleeps between three attempts


async def test_domain_limiter_caps_concurrency() -> None:
    limiter = DomainLimiter(per_domain=2)
    active = 0
    peak = 0

    async def worker() -> None:
        nonlocal active, peak
        async with limiter.slot("example.com"):
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1

    await asyncio.gather(*[worker() for _ in range(6)])
    assert peak == 2


def test_domain_limiter_rejects_zero() -> None:
    with pytest.raises(ValueError):
        DomainLimiter(0)
