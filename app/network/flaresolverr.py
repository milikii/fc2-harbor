"""FlareSolverr-protocol client for Byparr — P0.

Byparr speaks the FlareSolverr v1 API: ``POST /v1`` with
``{"cmd": "request.get", "url": ..., "maxTimeout": ...}`` and an answer whose
``solution.response`` is the rendered HTML. P0 only needs ``request.get``; no
sessions and no cookie reuse (that is P1).

Rules encoded here:

* one global request at a time (Byparr runs a browser, not a CDN edge) and one
  request per target domain, so two cards never fight over the same site;
* at most two attempts per URL, with a 5–10 s gap; the caller's HTTP timeout is
  75 s while Byparr itself gets ``maxTimeout=60000`` (Byparr reads values ≥ 1000
  as milliseconds, so this is 60 s);
* three consecutive failures for one domain open a 30-minute circuit;
* a challenge that is still a challenge after Byparr is a failure, not a result;
* the proxy is mandatory. The client checks that the proxy port is reachable
  before asking Byparr and never falls back to a direct connection. Byparr's own
  outbound traffic is configured separately (``PROXY_SERVER`` in compose).

Cookie values are never logged and never returned; only their names and whether
``cf_clearance`` is present, which is enough to diagnose the handshake.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.logging import error_detail, get_logger
from app.network.challenge import classify_page

_log = get_logger("byparr")

DEFAULT_MAX_TIMEOUT_MS = 60_000
DEFAULT_CALLER_TIMEOUT_SECONDS = 75.0
DEFAULT_ATTEMPTS = 2
DEFAULT_RETRY_GAP_SECONDS = (5.0, 10.0)
FAILURE_THRESHOLD = 3
COOLDOWN_SECONDS = 30 * 60.0
PROXY_CHECK_TIMEOUT_SECONDS = 3.0
PROXY_CACHE_SECONDS = 30.0

#: Failures worth a second attempt. A login wall and a bad URL are not.
_RETRYABLE_CODES = frozenset(
    {
        "byparr_timeout",
        "byparr_network",
        "byparr_http_error",
        "byparr_error",
        "byparr_invalid_response",
        "byparr_challenge_unresolved",
    }
)


class FlareSolverrError(RuntimeError):
    """A Byparr request that could not produce a usable page."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FlareSolverrResult:
    url: str
    status: int
    html: str
    #: Cookie **names** only. Values are credentials and never leave the client.
    cookie_names: tuple[str, ...]
    user_agent: str | None
    latency_ms: int
    attempts: int

    @property
    def has_cf_clearance(self) -> bool:
        return "cf_clearance" in self.cookie_names


class DomainCircuitBreaker:
    """Consecutive-failure circuit per target domain.

    After ``threshold`` failures the domain is open for ``cooldown_seconds``.
    When the cooldown expires the circuit goes half-open: one probe is allowed,
    a failure re-opens it immediately, a success closes it.
    """

    def __init__(
        self,
        *,
        threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if threshold < 1:
            raise ValueError("threshold must be at least 1")
        self.threshold = threshold
        self.cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}

    def _refresh(self, domain: str) -> bool:
        opened = self._opened_at.get(domain)
        if opened is None:
            return False
        if self._clock() - opened < self.cooldown_seconds:
            return True
        # Half-open: keep the failure count one short of the threshold, so the
        # next failure re-opens immediately and a success clears it.
        self._opened_at.pop(domain, None)
        self._failures[domain] = max(self._failures.get(domain, 0), self.threshold - 1)
        return False

    def allow(self, domain: str) -> bool:
        return not self._refresh(domain)

    def record_success(self, domain: str) -> None:
        self._failures.pop(domain, None)
        self._opened_at.pop(domain, None)

    def record_failure(self, domain: str) -> None:
        failures = self._failures.get(domain, 0) + 1
        self._failures[domain] = failures
        if failures >= self.threshold:
            self._opened_at[domain] = self._clock()

    def snapshot(self) -> dict[str, dict[str, float | int | bool]]:
        now = self._clock()
        result: dict[str, dict[str, float | int | bool]] = {}
        for domain in set(self._failures) | set(self._opened_at):
            opened = self._opened_at.get(domain)
            result[domain] = {
                "failures": self._failures.get(domain, 0),
                "open": opened is not None and now - opened < self.cooldown_seconds,
                "retry_in_seconds": (
                    max(0.0, self.cooldown_seconds - (now - opened)) if opened is not None else 0.0
                ),
            }
        return result


class FlareSolverrClient:
    """One shared Byparr client for every provider that needs it."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = DEFAULT_CALLER_TIMEOUT_SECONDS,
        max_timeout_ms: int = DEFAULT_MAX_TIMEOUT_MS,
        attempts: int = DEFAULT_ATTEMPTS,
        retry_gap_seconds: tuple[float, float] = DEFAULT_RETRY_GAP_SECONDS,
        failure_threshold: int = FAILURE_THRESHOLD,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
        sleeper: Callable[[float], Any] | None = None,
        rng: Any = None,
        proxy_checker: Callable[[str], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not base_url:
            raise ValueError("Byparr base URL must not be empty")
        if attempts < 1:
            raise ValueError("attempts must be at least 1")
        self.base_url = base_url.rstrip("/")
        self.max_timeout_ms = max_timeout_ms
        self._timeout = timeout_seconds
        self._attempts = attempts
        self._retry_gap = retry_gap_seconds
        self._sleep = sleeper or asyncio.sleep
        self._rng = rng or random.Random()  # noqa: S311 - pacing jitter, not crypto
        self._clock = clock
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_seconds),
            # Byparr is a LAN service; ambient proxy variables must not reroute it.
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )
        self._global = asyncio.Semaphore(1)
        self._domain_locks: dict[str, asyncio.Lock] = {}
        self._breaker = DomainCircuitBreaker(
            threshold=failure_threshold,
            cooldown_seconds=cooldown_seconds,
            clock=clock,
        )
        self._proxy_checker = proxy_checker or self._tcp_proxy_check
        self._proxy_checked_at: dict[str, float] = {}
        self._stats: dict[str, int] = {
            "requests": 0,
            "successes": 0,
            "failures": 0,
            "challenges": 0,
        }

    # -- public API ---------------------------------------------------------

    @property
    def budget_seconds(self) -> float:
        """Worst-case wall time one URL may spend inside Byparr."""

        return self._attempts * self._timeout + self._retry_gap[1] + 10.0

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def circuit_state(self) -> dict[str, dict[str, float | int | bool]]:
        return self._breaker.snapshot()

    async def request_get(
        self,
        url: str,
        *,
        proxy_url: str | None,
        required_selectors: Iterable[str] = (),
    ) -> FlareSolverrResult:
        """Fetch ``url`` through Byparr, with retry, pacing and a circuit.

        ``proxy_url`` is the source's proxy profile. It is mandatory: a provider
        without a proxy must not reach Byparr, because Byparr would then be the
        only thing standing between the request and a direct connection.
        """

        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise FlareSolverrError("invalid_url", f"not an http(s) URL: {url!r}")
        domain = parts.hostname.lower()

        if not proxy_url:
            raise FlareSolverrError(
                "proxy_required",
                f"{domain}: a proxy profile is required before Byparr may be used",
            )
        if not self._breaker.allow(domain):
            raise FlareSolverrError(
                "byparr_circuit_open",
                f"{domain}: circuit open after {self._breaker.threshold} failures",
            )

        await self._ensure_proxy(proxy_url)

        last_error: FlareSolverrError | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                result = await self._one(
                    url,
                    domain=domain,
                    required_selectors=required_selectors,
                    attempt=attempt,
                )
            except FlareSolverrError as exc:
                last_error = exc
                if exc.code == "byparr_login_wall":
                    # A login wall is a page, not a solver failure: retrying or
                    # counting it against the domain would be misleading.
                    raise
                if exc.code not in _RETRYABLE_CODES:
                    raise
                _log.warning(
                    "byparr.attempt_failed",
                    domain=domain,
                    attempt=attempt,
                    max_attempts=self._attempts,
                    error_code=exc.code,
                    error_detail=error_detail(exc),
                )
                if attempt < self._attempts:
                    await self._sleep(self._gap())
                    continue
                self._breaker.record_failure(domain)
                self._stats["failures"] += 1
                if exc.code == "byparr_challenge_unresolved":
                    self._stats["challenges"] += 1
                raise
            self._breaker.record_success(domain)
            self._stats["requests"] += 1
            self._stats["successes"] += 1
            return result

        # Unreachable: the loop returns or raises.
        raise last_error or FlareSolverrError("byparr_error", "Byparr request failed")

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- internals ----------------------------------------------------------

    def _gap(self) -> float:
        return float(self._rng.uniform(*self._retry_gap))

    async def _one(
        self,
        url: str,
        *,
        domain: str,
        required_selectors: Iterable[str],
        attempt: int,
    ) -> FlareSolverrResult:
        # Global first, then per-domain: the lock order is always the same, so
        # two providers cannot deadlock each other.
        async with self._global:
            lock = self._domain_locks.setdefault(domain, asyncio.Lock())
            async with lock:
                started = self._clock()
                try:
                    response = await self._client.post(
                        f"{self.base_url}/v1",
                        json={
                            "cmd": "request.get",
                            "url": url,
                            "maxTimeout": self.max_timeout_ms,
                        },
                    )
                except httpx.TimeoutException as exc:
                    raise FlareSolverrError(
                        "byparr_timeout",
                        f"Byparr did not answer within {self._timeout:g}s",
                    ) from exc
                except httpx.HTTPError as exc:
                    raise FlareSolverrError(
                        "byparr_network",
                        f"Byparr request failed: {type(exc).__name__}",
                    ) from exc

                latency_ms = int((self._clock() - started) * 1000)
                payload = self._decode(response)
                solution = payload.get("solution")
                if not isinstance(solution, dict):
                    raise FlareSolverrError(
                        "byparr_invalid_response", "response has no solution object"
                    )

                status = solution.get("status")
                html = solution.get("response")
                if not isinstance(status, int) or not isinstance(html, str):
                    raise FlareSolverrError(
                        "byparr_invalid_response", "solution has no status/response"
                    )

                verdict = classify_page(
                    status=status,
                    headers=solution.get("headers")
                    if isinstance(solution.get("headers"), dict)
                    else None,
                    html=html,
                    required_selectors=required_selectors,
                )
                if verdict.kind == "login_wall":
                    raise FlareSolverrError(
                        "byparr_login_wall", f"{domain}: login wall, not a challenge"
                    )
                if verdict.is_challenge:
                    raise FlareSolverrError(
                        "byparr_challenge_unresolved",
                        f"{domain}: still a challenge ({verdict.reason or 'unknown'})",
                    )

                cookies = solution.get("cookies")
                names: list[str] = []
                if isinstance(cookies, list):
                    for item in cookies:
                        if isinstance(item, dict) and item.get("name"):
                            names.append(str(item["name"]))
                user_agent = solution.get("userAgent")
                result = FlareSolverrResult(
                    url=str(solution.get("url") or url),
                    status=status,
                    html=html,
                    cookie_names=tuple(names),
                    user_agent=str(user_agent) if user_agent else None,
                    latency_ms=latency_ms,
                    attempts=attempt,
                )
                _log.info(
                    "byparr.request",
                    domain=domain,
                    status=status,
                    latency_ms=latency_ms,
                    attempt=attempt,
                    html_bytes=len(html),
                    cookie_count=len(names),
                    has_cf_clearance=result.has_cf_clearance,
                )
                return result

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 500:
            raise FlareSolverrError(
                "byparr_http_error", f"Byparr answered {response.status_code}"
            )
        if response.status_code >= 400:
            raise FlareSolverrError(
                "byparr_http_error", f"Byparr answered {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlareSolverrError(
                "byparr_invalid_response", "Byparr answer is not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise FlareSolverrError("byparr_invalid_response", "Byparr answer is not an object")
        if payload.get("status") != "ok":
            message = str(payload.get("message") or "unknown error")[:300]
            raise FlareSolverrError("byparr_error", message)
        return payload

    async def _ensure_proxy(self, proxy_url: str) -> None:
        now = self._clock()
        checked = self._proxy_checked_at.get(proxy_url)
        if checked is not None and now - checked < PROXY_CACHE_SECONDS:
            return
        try:
            await self._proxy_checker(proxy_url)
        except Exception as exc:
            host = urlsplit(proxy_url).hostname or "unknown"
            raise FlareSolverrError(
                "proxy_unavailable",
                f"proxy {host} is unreachable: {type(exc).__name__}",
            ) from exc
        self._proxy_checked_at[proxy_url] = now

    @staticmethod
    async def _tcp_proxy_check(proxy_url: str) -> None:
        """Cheap liveness probe: the proxy port accepts a TCP connection."""

        parts = urlsplit(proxy_url)
        host = parts.hostname
        if not host:
            raise ValueError("proxy URL has no host")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=PROXY_CHECK_TIMEOUT_SECONDS
        )
        writer.close()
        with contextlib.suppress(Exception):  # close races are irrelevant here
            await writer.wait_closed()
