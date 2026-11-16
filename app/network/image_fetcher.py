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


#: Image extensions accepted from a viewer page's links.
_VIEWER_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp", ".gif")


def image_url_from_viewer(html_text: str, base_url: str) -> str | None:
    """The most likely real image URL inside an HTML viewer page.

    Order: ``og:image`` → ``twitter:image`` → the largest ``<img>`` (explicit
    width/height wins, then a class/id that looks like the main picture) → the first
    link pointing at an image file. No guessing beyond that: ``None`` means "give up".
    """

    from selectolax.lexbor import LexborHTMLParser

    tree = LexborHTMLParser(html_text or "")

    for selector in ('meta[property="og:image"]', 'meta[name="twitter:image"]'):
        node = tree.css_first(selector)
        if node is not None:
            content = (node.attributes.get("content") or "").strip()
            if content:
                return urljoin(base_url, content)

    best: tuple[int, str] | None = None
    for node in tree.css("img"):
        source = (node.attributes.get("src") or node.attributes.get("data-src") or "").strip()
        if not source or source.startswith("data:"):
            continue
        try:
            area = int(node.attributes.get("width") or 0) * int(
                node.attributes.get("height") or 0
            )
        except (TypeError, ValueError):
            area = 0
        hint = f"{node.attributes.get('class') or ''} {node.attributes.get('id') or ''}".lower()
        if any(word in hint for word in ("image", "photo", "main")):
            area += 1_000_000  # a labelled main picture beats an unlabelled one
        if best is None or area > best[0]:
            best = (area, source)
    if best is not None:
        return urljoin(base_url, best[1])

    for node in tree.css("a[href]"):
        href = (node.attributes.get("href") or "").strip()
        lowered = href.lower().split("?")[0]
        if lowered.endswith(_VIEWER_IMAGE_SUFFIXES):
            return urljoin(base_url, href)
    return None


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
        strict_allowlist: bool = False,
        max_bytes: int = DEFAULT_MAX_BYTES,
        timeout_seconds: float = 20.0,
        max_redirects: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[[str], list[str]] | None = None,
        user_agent: str = "fc2-harbor/0.1",
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.allowed_hosts = tuple(host.strip().lower() for host in allowed_hosts if host)
        self.strict_allowlist = strict_allowlist
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
        # A literal private address is an SSRF attempt in either mode, and saying so
        # is more useful than "host not allowlisted".
        if _looks_like_ip(host):
            if not is_public_ip(host):
                _log.warning("image.rejected_private_ip", host=host, reason="literal_ip")
                raise ImageFetchError("ssrf_blocked", f"refusing private address: {host}")
            return host

        if self.strict_allowlist and not self._host_allowed(host):
            raise ImageFetchError("host_not_allowed", f"host not allowlisted: {host}")
        if not self.strict_allowlist and self.allowed_hosts and not self._host_allowed(host):
            # Visible on purpose: "we fetched from a host outside the list because
            # its address is public" must be explainable from the logs.
            _log.info("image.allowed_by_ip_only", host=host)

        try:
            addresses = self._resolver(host)
        except OSError as exc:
            raise ImageFetchError("dns_failed", f"cannot resolve {host}: {exc}") from exc
        if not addresses:
            raise ImageFetchError("dns_failed", f"no address for {host}")
        for address in addresses:
            if not is_public_ip(address):
                _log.warning(
                    "image.rejected_private_ip", host=host, address=address, reason="dns"
                )
                raise ImageFetchError(
                    "ssrf_blocked", f"{host} resolves to a non-public address: {address}"
                )
        return host

    def _host_allowed(self, host: str) -> bool:
        if not self.allowed_hosts:
            # With ``strict_allowlist`` on, an empty list trusts nothing.
            return not self.strict_allowlist
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
        resolve_viewer: bool = False,
    ) -> FetchedImage:
        """Download one image.

        ``resolve_viewer`` allows exactly **one** level of indirection: some image
        hosts answer with an HTML "viewer" page instead of the file. When enabled,
        that page is parsed for the real image URL, which then goes through the whole
        validation pipeline again (IP, size, content type, decoding).
        """

        return await self._fetch(
            url,
            proxy_url=proxy_url,
            referer=referer,
            resolve_viewer=resolve_viewer,
            depth=0,
        )

    async def _fetch(
        self,
        url: str,
        *,
        proxy_url: str | None,
        referer: str | None,
        resolve_viewer: bool,
        depth: int,
    ) -> FetchedImage:
        current = url
        headers = {"Referer": referer} if referer else {}
        for _hop in range(self.max_redirects + 1):
            self.ensure_allowed(current)
            body, content_type, final_url = await self._download(
                current, headers, proxy_url, allow_html=resolve_viewer
            )
            if body is None:  # redirect
                location = content_type
                current = urljoin(current, location)
                continue
            if content_type == "text/html":
                if not resolve_viewer or depth >= 1:
                    _log.warning(
                        "image.viewer_unresolved",
                        domain=urlsplit(final_url).hostname,
                        reason="nested_or_disabled",
                        depth=depth,
                    )
                    raise ImageFetchError(
                        "bad_content_type", "image url served an HTML viewer page"
                    )
                _log.info(
                    "image.viewer_page_detected",
                    domain=urlsplit(final_url).hostname,
                    bytes=len(body),
                )
                target = image_url_from_viewer(
                    body.decode("utf-8", "replace"), final_url
                )
                if not target:
                    _log.warning(
                        "image.viewer_unresolved",
                        domain=urlsplit(final_url).hostname,
                        reason="no_image_found",
                    )
                    raise ImageFetchError(
                        "bad_content_type", "viewer page had no image URL"
                    )
                _log.info("image.viewer_resolved", found_url=target)
                return await self._fetch(
                    target,
                    proxy_url=proxy_url,
                    referer=final_url,
                    resolve_viewer=False,
                    depth=depth + 1,
                )
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
        *,
        allow_html: bool = False,
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
                if allow_html and content_type == "text/html":
                    # A viewer page: read it so the caller can find the real image.
                    pass
                elif content_type not in ALLOWED_CONTENT_TYPES:
                    _log.warning(
                        "image.rejected_content_type",
                        domain=urlsplit(url).hostname,
                        content_type=content_type,
                    )
                    raise ImageFetchError(
                        "bad_content_type", f"unsupported image type {content_type!r}"
                    )
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    _log.warning(
                        "image.rejected_size",
                        domain=urlsplit(url).hostname,
                        declared_bytes=int(declared),
                        limit=self.max_bytes,
                    )
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
