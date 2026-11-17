"""Byparr client policy: payload, timeout, retries, pacing, circuit, proxy.

No real network: respx (or an in-process transport) answers the Byparr API.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx
from app.network.flaresolverr import (
    DEFAULT_MAX_TIMEOUT_MS,
    FlareSolverrClient,
    FlareSolverrError,
)

BASE = "http://byparr:8191"
URL = "https://missav123.com/search/FC2-PPV-4559944"
PROXY = "http://127.0.0.1:7890"
SOLVED = '<html><body><div class="thumbnail">FC2-PPV-4559944</div></body></html>'


class FakeRng:
    def uniform(self, low: float, high: float) -> float:
        return low


def make_client(**kwargs: Any) -> tuple[FlareSolverrClient, list[float]]:
    delays: list[float] = []

    async def sleeper(seconds: float) -> None:
        delays.append(seconds)

    async def proxy_ok(_url: str) -> None:
        return None

    options: dict[str, Any] = {
        "proxy_checker": proxy_ok,
        "sleeper": sleeper,
        "rng": FakeRng(),
    }
    options.update(kwargs)
    return FlareSolverrClient(BASE, **options), delays


def answer(
    *,
    status: int = 200,
    html: str = SOLVED,
    cookies: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "message": "Success",
        "solution": {
            "url": URL,
            "status": status,
            "response": html,
            "cookies": cookies
            if cookies is not None
            else [
                {"name": "cf_clearance", "value": "TOP-SECRET-COOKIE"},
                {"name": "user_uuid", "value": "also-secret"},
            ],
            "userAgent": "Mozilla/5.0",
        },
    }


@respx.mock
async def test_posts_flaresolverr_payload_and_returns_page(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1").respond(200, json=answer())
    client, _ = make_client()
    try:
        result = await client.request_get(
            URL, proxy_url=PROXY, required_selectors=(".thumbnail",)
        )
    finally:
        await client.aclose()

    payload = json.loads(route.calls[0].request.content)
    assert payload == {
        "cmd": "request.get",
        "url": URL,
        "maxTimeout": DEFAULT_MAX_TIMEOUT_MS,
    }
    assert result.status == 200
    assert "FC2-PPV-4559944" in result.html
    assert result.cookie_names == ("cf_clearance", "user_uuid")
    assert result.has_cf_clearance is True
    # Cookie values are credentials: they must not be part of the result object.
    assert "TOP-SECRET-COOKIE" not in repr(result)


@respx.mock
async def test_second_attempt_waits_five_to_ten_seconds(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1").mock(
        side_effect=[httpx.Response(502), httpx.Response(200, json=answer())]
    )
    client, delays = make_client()
    try:
        result = await client.request_get(URL, proxy_url=PROXY)
    finally:
        await client.aclose()

    assert len(route.calls) == 2
    assert result.attempts == 2
    assert delays == [5.0]


@respx.mock
async def test_three_consecutive_failures_open_circuit_for_30_minutes(
    respx_mock: respx.MockRouter,
) -> None:
    clock = [1_000.0]
    route = respx_mock.post(f"{BASE}/v1").respond(502)
    client, _ = make_client(clock=lambda: clock[0], attempts=1)
    try:
        for _ in range(3):
            with pytest.raises(FlareSolverrError) as excinfo:
                await client.request_get(URL, proxy_url=PROXY)
            assert excinfo.value.code == "byparr_http_error"

        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY)
        assert excinfo.value.code == "byparr_circuit_open"
        assert len(route.calls) == 3

        # After the cooldown the circuit is half-open: one probe is allowed.
        clock[0] += 30 * 60
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY)
        assert excinfo.value.code == "byparr_http_error"
        assert len(route.calls) == 4

        # A failure during the probe re-opens it immediately.
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY)
        assert excinfo.value.code == "byparr_circuit_open"
    finally:
        await client.aclose()


@respx.mock
async def test_success_resets_consecutive_failures(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1").mock(
        side_effect=[
            httpx.Response(502),
            httpx.Response(502),
            httpx.Response(200, json=answer()),
            httpx.Response(502),
            httpx.Response(502),
            httpx.Response(200, json=answer()),
        ]
    )
    client, _ = make_client(attempts=1)
    try:
        for _ in range(2):
            with pytest.raises(FlareSolverrError):
                await client.request_get(URL, proxy_url=PROXY)
        await client.request_get(URL, proxy_url=PROXY)
        for _ in range(2):
            with pytest.raises(FlareSolverrError):
                await client.request_get(URL, proxy_url=PROXY)
        await client.request_get(URL, proxy_url=PROXY)
    finally:
        await client.aclose()

    assert len(route.calls) == 6


async def test_global_concurrency_is_one_across_domains() -> None:
    active = 0
    peak = 0

    class Handler(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.02)
            active -= 1
            return httpx.Response(
                200,
                content=json.dumps(answer()).encode(),
                headers={"content-type": "application/json"},
            )

    client, _ = make_client(transport=Handler())
    try:
        await asyncio.gather(
            client.request_get("https://a.example/x", proxy_url=PROXY),
            client.request_get("https://b.example/x", proxy_url=PROXY),
        )
    finally:
        await client.aclose()

    assert peak == 1


@respx.mock
async def test_requests_to_the_same_domain_are_serial(respx_mock: respx.MockRouter) -> None:
    active = 0
    peak = 0

    async def side_effect(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json=answer())

    respx_mock.post(f"{BASE}/v1").mock(side_effect=side_effect)
    client, _ = make_client()
    try:
        await asyncio.gather(
            client.request_get(URL, proxy_url=PROXY),
            client.request_get(URL, proxy_url=PROXY),
        )
    finally:
        await client.aclose()

    assert peak == 1


@respx.mock
async def test_no_proxy_means_no_byparr_call(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1").respond(200, json=answer())
    client, _ = make_client()
    try:
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=None)
    finally:
        await client.aclose()

    assert excinfo.value.code == "proxy_required"
    assert not route.called


@respx.mock
async def test_unreachable_proxy_stops_before_byparr(respx_mock: respx.MockRouter) -> None:
    route = respx_mock.post(f"{BASE}/v1").respond(200, json=answer())

    async def proxy_down(_url: str) -> None:
        raise OSError("connection refused")

    client, _ = make_client(proxy_checker=proxy_down)
    try:
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY)
    finally:
        await client.aclose()

    assert excinfo.value.code == "proxy_unavailable"
    assert not route.called


@respx.mock
async def test_unresolved_challenge_is_a_failure_not_a_result(
    respx_mock: respx.MockRouter,
) -> None:
    challenge = (
        "<html><head><title>Just a moment...</title></head>"
        "<body>Just a moment...</body></html>"
    )
    route = respx_mock.post(f"{BASE}/v1").respond(200, json=answer(status=403, html=challenge))
    client, delays = make_client()
    try:
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY, required_selectors=(".thumbnail",))
    finally:
        await client.aclose()

    assert excinfo.value.code == "byparr_challenge_unresolved"
    assert len(route.calls) == 2
    assert delays == [5.0]
    assert client.stats["challenges"] == 1


@respx.mock
async def test_login_wall_is_not_retried(respx_mock: respx.MockRouter) -> None:
    login = '<html><body><form action="/login"><input type="password"></form></body></html>'
    route = respx_mock.post(f"{BASE}/v1").respond(200, json=answer(html=login))
    client, delays = make_client()
    try:
        with pytest.raises(FlareSolverrError) as excinfo:
            await client.request_get(URL, proxy_url=PROXY, required_selectors=(".thumbnail",))
    finally:
        await client.aclose()

    assert excinfo.value.code == "byparr_login_wall"
    assert len(route.calls) == 1
    assert delays == []
