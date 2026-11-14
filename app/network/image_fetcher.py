"""Image download, validation and cache — spec 8.4 / 20.

Never hand a provider URL to Telegram (spec 8.4). Every image is fetched by the
backend through the routed proxy, then:

1. size-capped while streaming (``max_bytes``, plus ``Content-Length`` early exit);
2. content-type checked against an allowlist (SVG and HTML are rejected, so a
   "picture" cannot smuggle a script);
3. decoded with Pillow (format allowlist, pixel-bomb guard, sane dimensions);
4. stored in a content-addressed cache (``<sha256[:2]>/<sha256>.<ext>``) written
   atomically, so the same image is only downloaded once (spec 7.2 rule 6);
5. returned with size, dimensions and hash for the ``assets`` row.

SSRF defence: only http(s), only allowlisted hosts, every resolved address must be
public (no loopback, private, link-local, metadata or reserved ranges), and every
redirect hop is re-validated instead of being followed blindly.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from app.logging import get_logger

_log = get_logger("images")

ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif"}
)
FORMAT_EXTENSION: Mapping[str, str] = {
    "JPEG": "jpg",
    "PNG": "png",
    "WEBP": "webp",
    "GIF": "gif",
}
MAX_PIXELS = 40_000_000
MAX_DIMENSION = 20_000
DEFAULT_MAX_BYTES = 20 * 1024 * 1024
CHUNK = 64 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

#: Pillow refuses images above this; anything above ~2x raises a hard error.
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


class ImageFetchError(RuntimeError):
    """A rejected or failed image download. ``code`` is stable for logs/tests."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FetchedImage:
    url: str
    final_url: str
    sha256: str
    path: Path
    content_type: str
    width: int
    height: int
    size_bytes: int
    cdn_host: str


def is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]) for info in infos})


class ImageFetcher:
    def __init__(
        self,
        *,
        cache_dir: Path,
        allowed_hosts: Iterable[str] = (),
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_seconds: float = 20.0,
        max_redirects: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        user_agent: str = "fc2-harbor/0.1",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.allowed_hosts = tuple(host.strip().lower() for host in allowed_hosts if host)
        self.max_bytes = max_bytes
        self.max_redirects = max_redirects
        self._timeout = httpx.Timeout(timeout_seconds)
        self._transport = transport
        self._resolver = resolver or _default_resolver
        self._user_agent = user_agent
        self._clients: dict[str | None, httpx.AsyncClient] = {}

    # -- lifecycle ----------------------------------------------------------

    def _client_for(self, proxy_url: str | None) -> httpx.AsyncClient:
        client = self._clients.get(proxy_url)
        if client is None:
            client = httpx.AsyncClient(
                timeout=self._timeout,
                headers={"User-Agent": self._user_agent},
                # Redirects are handled here so every hop can be re-validated.
                follow_redirects=False,
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

    # -- validation ---------------------------------------------------------

    def ensure_allowed(self, url: str) -> str:
        """Validate scheme, host allowlist and resolved addresses (SSRF guard)."""

        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"}:
            raise ImageFetchError("bad_scheme", f"unsupported image scheme: {parts.scheme!r}")
        host = (parts.hostname or "").strip().lower()
        if not host:
            raise ImageFetchError("bad_url", f"image url has no host: {url!r}")
        if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
            raise ImageFetchError("ssrf_blocked", f"refusing local host: {host}")
        if not self._host_allowed(host):
            raise ImageFetchError("host_not_allowed", f"host not allowlisted: {host}")

        if _looks_like_ip(host):
            if not is_public_ip(host):
                raise ImageFetchError("ssrf_blocked", f"refusing private address: {host}")
            return host

        try:
            addresses = self._resolver(host)
        except OSError as exc:
            raise ImageFetchError("dns_failed", f"cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise ImageFetchError("dns_failed", f"no address for {host}")
        for address in addresses:
            if not is_public_ip(address):
                raise ImageFetchError(
                    "ssrf_blocked", f"{host} resolves to a non-public address: {address}"
                )
        return host

    def _host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            # No allowlist configured means "no host is implicitly trusted"; the
            # caller must derive it from the provider configuration.
            raise ImageFetchError("no_allowlist", "image_fetch.allowed_hosts is empty")
        return any(
            host == allowed or host.endswith("." + allowed) for allowed in self.allowed_hosts
        )

    # -- download -----------------------------------------------------------

    async def fetch(
        self,
        url: str,
        *,
        proxy_url: str | None = None,
        referer: str | None = None,
    ) -> FetchedImage:
        current = url
        headers = {"Referer": referer} if referer else {}
        for _hop in range(self.max_redirects + 1):
            self.ensure_allowed(current)
            body, content_type, final_url = await self._download(current, headers, proxy_url)
            if body is None:  # redirect
                location = content_type
                current = urljoin(current, location)
                continue
            return self._validate_and_store(
                requested=url,
                final_url=final_url,
                content_type=content_type,
                body=body,
            )
        raise ImageFetchError("too_many_redirects", f"more than {self.max_redirects} redirects")

    async def _download(
        self,
        url: str,
        headers: Mapping[str, str],
        proxy_url: str | None,
    ) -> tuple[bytes | None, str, str]:
        started = time.monotonic()
        client = self._client_for(proxy_url)
        try:
            async with client.stream("GET", url, headers=dict(headers)) as response:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise ImageFetchError("bad_redirect", f"redirect without Location: {url}")
                    return None, location, url
                if response.status_code >= 400:
                    raise ImageFetchError(
                        f"http_{response.status_code}", f"image request failed: {url}"
                    )

                content_type = (
                    (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                )
                if content_type not in ALLOWED_CONTENT_TYPES:
                    raise ImageFetchError(
                        "bad_content_type", f"unsupported image type {content_type!r}"
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise ImageFetchError(
                        "too_large", f"declared size {declared} exceeds {self.max_bytes}"
                    )

                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes(CHUNK):
                    total += len(chunk)
                    if total > self.max_bytes:
                        raise ImageFetchError("too_large", f"image exceeds {self.max_bytes} bytes")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise ImageFetchError("network", f"image request failed: {type(exc).__name__}") from exc

        _log.info(
            "image.downloaded",
            domain=urlsplit(url).hostname,
            bytes=total,
            latency_ms=int((time.monotonic() - started) * 1000),
            status=200,
        )
        return b"".join(chunks), content_type, str(url)

    # -- decode + cache -----------------------------------------------------

    def _validate_and_store(
        self,
        *,
        requested: str,
        final_url: str,
        content_type: str,
        body: bytes,
    ) -> FetchedImage:
        if not body:
            raise ImageFetchError("empty", "image body is empty")

        try:
            with Image.open(BytesIO(body)) as probe:
                image_format = (probe.format or "").upper()
                width, height = probe.size
                probe.verify()
            with Image.open(BytesIO(body)) as decoded:
                width, height = decoded.size
        except Image.DecompressionBombError as exc:
            raise ImageFetchError("decompression_bomb", "image exceeds the pixel limit") from exc
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise ImageFetchError("not_an_image", f"cannot decode image: {exc}") from exc

        if image_format not in FORMAT_EXTENSION:
            raise ImageFetchError("bad_format", f"unsupported image format {image_format!r}")
        if width <= 0 or height <= 0 or width > MAX_DIMENSION or height > MAX_DIMENSION:
            raise ImageFetchError("bad_dimensions", f"unreasonable image size {width}x{height}")

        digest = hashlib.sha256(body).hexdigest()
        extension = FORMAT_EXTENSION[image_format]
        target = self.cache_dir / digest[:2] / f"{digest}.{extension}"
        self._write_atomic(target, body)

        return FetchedImage(
            url=requested,
            final_url=final_url,
            sha256=digest,
            path=target,
            content_type=content_type,
            width=width,
            height=height,
            size_bytes=len(body),
            cdn_host=urlsplit(final_url).hostname or "",
        )

    @staticmethod
    def _write_atomic(target: Path, body: bytes) -> None:
        if target.exists() and target.stat().st_size == len(body):
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(body)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, target)
        except OSError:
            Path(temp_name).unlink(missing_ok=True)
            raise


def _looks_like_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True
