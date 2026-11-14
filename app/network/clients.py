"""Explicit HTTP client for external sites — spec 6.2 / 8.3.

Rules encoded here:

* **No global proxy.** ``trust_env=False``; a proxy is passed per request (the
  provider/CDN routing of M3 builds on this), so ``HTTP_PROXY`` can never leak
  NAS-local or downloader traffic.
* **Per-domain concurrency** (default 2) and rate limiting, keyed by hostname.
* **Retry only idempotent GETs**, at most ``retries`` times, exponential backoff
  with jitter, and ``Retry-After`` is honoured (seconds or HTTP date, capped).
* **Conditional requests**: the caller supplies ``If-None-Match`` /
  ``If-Modified-Since`` and gets a 304 back without a body.
* Strict timeouts; a failed request raises, it never kills the process (spec 20).
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.logging import get_logger

_log = get_logger("network")

DEFAULT_USER_AGENT = "fc2-harbor/0.1"
BACKOFF_BASE_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 8.0
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


class HttpError(RuntimeError):
    """An HTTP request that failed permanently."""

    def __init__(self, message: str, *, status: int | None = None, url: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.url = url


class HttpNetworkError(HttpError):
    """The host could not be reached (DNS, TLS, timeout, reset)."""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    url: str
    status: int
    content: bytes
    headers: Mapping[str, str]

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def text(self) -> str:
        charset = "utf-8"
        content_type = self.headers.get("content-type", "")
        if "charset=" in content_type.lower():
            charset = content_type.lower().split("charset=", 1)[1].split(";")[0].strip() or "utf-8"
        try:
            return self.content.decode(charset, errors="replace")
        except LookupError:
            return self.content.decode("utf-8", errors="replace")

    def header(self, name: str) -> str | None:
        for key, value in self.headers.items():
            if key.lower() == name.lower():
                return value
        return None


class DomainLimiter:
    """One semaphore per hostname so concurrency never exceeds the spec limit."""

    def __init__(self, per_domain: int) -> None:
        if per_domain < 1:
            raise ValueError("per_domain concurrency must be at least 1")
        self._per_domain = per_domain
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    @asynccontextmanager
    async def slot(self, domain: str) -> AsyncIterator[None]:
        semaphore = self._semaphores.setdefault(domain, asyncio.Semaphore(self._per_domain))
        async with semaphore:
            yield


def parse_retry_after(value: str | None, *, cap_seconds: float) -> float | None:
    """Parse ``Retry-After`` as delta-seconds or an HTTP date."""

    if not value:
        return None
    candidate = value.strip()
    try:
        seconds = float(candidate)
    except ValueError:
        try:
            when = parsedate_to_datetime(candidate)
        except (TypeError, ValueError):
            return None
        if when is None:
            return None
        seconds = when.timestamp() - time.time()
    if seconds < 0:
        return 0.0
    return min(seconds, cap_seconds)


class HttpClient:
    """Small async HTTP client with the retry/limit policy from the spec."""

    def __init__(
        self,
        *,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = 20.0,
        retries: int = 2,
        per_domain_concurrency: int = 2,
        max_retry_after_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Any = None,
        metrics: Any = None,
    ) -> None:
        self._user_agent = user_agent
        self._timeout = httpx.Timeout(timeout_seconds)
        self._retries = max(0, retries)
        self._limiter = DomainLimiter(per_domain_concurrency)
        self._max_retry_after = max_retry_after_seconds
        self._transport = transport
        self._sleep = sleeper or asyncio.sleep
        self._metrics = metrics
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    # -- lifecycle ----------------------------------------------------------

    def _client_for(self, proxy_url: str | None) -> httpx.AsyncClient:
        client = self._clients.get(proxy_url)
        if client is None:
            client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                follow_redirects=True,
                max_redirects=5,
                # Spec 8.2: explicit routing only, never the ambient environment.
                trust_env=False,
                proxy=proxy_url,
                transport=self._transport,
            )
            self._clients[proxy_url] = client
        return client

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # -- requests -----------------------------------------------------------

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        proxy_url: str | None = None,
        proxy_label: str | None = None,
        proxy_region: str | None = None,
    ) -> HttpResponse:
        """GET with retries. Only GET is supported: it is the only idempotent verb."""

        domain = urlsplit(url).hostname or "unknown"
        last_error: Exception | None = None

        for attempt in range(self._retries + 1):
            started = time.monotonic()
            try:
                async with self._limiter.slot(domain):
                    response = await self._client_for(proxy_url).get(
                        url, headers=dict(headers or {})
                    )
            except httpx.HTTPError as exc:
                last_error = exc
                if self._metrics is not None and proxy_label:
                    self._metrics.record_proxy(
                        proxy_label,
                        region=proxy_region,
                        ok=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        error=type(exc).__name__,
                    )
                _log.warning(
                    "http.request_failed",
                    domain=domain,
                    attempt=attempt + 1,
                    error_code=type(exc).__name__,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                if attempt < self._retries:
                    await self._sleep(self._backoff(attempt))
                    continue
                raise HttpNetworkError(
                    f"request to {domain} failed: {type(exc).__name__}", url=url
                ) from exc

            latency_ms = int((time.monotonic() - started) * 1000)
            if self._metrics is not None and proxy_label:
                self._metrics.record_proxy(
                    proxy_label,
                    region=proxy_region,
                    ok=response.status_code < 500,
                    latency_ms=latency_ms,
                    error=None if response.status_code < 500 else f"http_{response.status_code}",
                )
            _log.info(
                "http.request",
                domain=domain,
                status=response.status_code,
                latency_ms=latency_ms,
                attempt=attempt + 1,
            )

            if response.status_code in RETRYABLE_STATUS:
                delay = parse_retry_after(
                    response.headers.get("Retry-After"), cap_seconds=self._max_retry_after
                )
                if attempt < self._retries:
                    await self._sleep(delay if delay is not None else self._backoff(attempt))
                    continue
                raise HttpError(
                    f"{domain} answered {response.status_code} after {attempt + 1} attempts",
                    status=response.status_code,
                    url=url,
                )

            if response.status_code == 304:
                return HttpResponse(
                    url=url, status=304, content=b"", headers=dict(response.headers)
                )

            if response.status_code >= 400:
                raise HttpError(
                    f"{domain} answered {response.status_code}",
                    status=response.status_code,
                    url=url,
                )

            return HttpResponse(
                url=str(response.url),
                status=response.status_code,
                content=response.content,
                headers=dict(response.headers),
            )

        # Unreachable: the loop either returns or raises.
        raise HttpNetworkError(f"request to {domain} failed", url=url) from last_error

    @asynccontextmanager
    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        proxy_url: str | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Streaming GET for callers that must cap the body (images).

        Redirects are followed by the client here; the image fetcher uses its own
        client so it can validate every hop itself.
        """

        domain = urlsplit(url).hostname or "unknown"
        async with self._limiter.slot(domain):
            async with self._client_for(proxy_url).stream(
                "GET", url, headers=dict(headers or {})
            ) as response:
                yield response

    def _backoff(self, attempt: int) -> float:
        base = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
        # Jitter for retry spacing, not a security decision.
        return base + random.uniform(0, base / 2)  # noqa: S311
