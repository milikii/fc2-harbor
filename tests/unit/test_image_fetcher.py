"""Image pipeline — spec 8.4 / 20.

Every case uses a mock transport and a fake resolver: no network, no real image
hosts. The security cases are the point: SSRF, content-type spoofing, oversized
bodies and redirects to private addresses.
"""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import httpx
import pytest
from app.network.image_fetcher import FetchedImage, ImageFetcher, ImageFetchError
from PIL import Image

PUBLIC_IP = "93.184.216.34"


def png_bytes(width: int = 12, height: int = 8) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def make_fetcher(tmp_path: Path, handler, **kwargs: object) -> ImageFetcher:
    options: dict[str, object] = {
        "cache_dir": tmp_path / "cache",
        "allowed_hosts": ["img.example.com"],
        "transport": httpx.MockTransport(handler),
        "resolver": lambda host: [PUBLIC_IP],
    }
    options.update(kwargs)
    return ImageFetcher(**options)  # type: ignore[arg-type]


async def test_valid_png_is_cached_with_metadata(tmp_path: Path) -> None:
    body = png_bytes()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        image = await fetcher.fetch("https://img.example.com/a.png")
        again = await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()

    assert isinstance(image, FetchedImage)
    assert image.width == 12 and image.height == 8
    assert image.size_bytes == len(body)
    assert image.path.exists()
    assert image.path.read_bytes() == body
    assert image.path.name == f"{image.sha256}.png"
    assert image.cdn_host == "img.example.com"
    # The cache is content-addressed, so a re-download lands on the same file.
    # URL-level dedupe happens one layer up, in the asset store, which knows the
    # previous sha256 for a normalised URL and skips the request entirely.
    assert again.sha256 == image.sha256
    assert again.path == image.path
    assert calls["n"] == 2


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "image/svg+xml", "application/octet-stream", ""],
)
async def test_non_image_content_types_are_rejected(tmp_path: Path, content_type: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<svg/>", headers={"content-type": content_type})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "bad_content_type"


async def test_spoofed_content_type_is_rejected_by_pillow(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not an image", headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "not_an_image"


async def test_oversized_body_is_aborted(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=png_bytes(64, 64), headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler, max_bytes=64)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code in {"too_large"}


async def test_declared_content_length_is_checked_early(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=png_bytes(),
            headers={"content-type": "image/png", "content-length": "999999999"},
        )

    fetcher = make_fetcher(tmp_path, handler, max_bytes=1024)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "too_large"


@pytest.mark.parametrize(
    ("url", "code"),
    [
        # The IP guard runs in both modes, so a loopback literal is SSRF, not a
        # host-list problem.
        ("https://127.0.0.1/a.png", "ssrf_blocked"),
        ("https://localhost/a.png", "ssrf_blocked"),
        ("file:///etc/passwd", "bad_scheme"),
        # A public host outside the list is refused only in strict mode.
        ("https://evil.example.net/a.png", "host_not_allowed"),
    ],
)
async def test_url_guards(tmp_path: Path, url: str, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(tmp_path, handler, strict_allowlist=True)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch(url)
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == code


async def test_host_resolving_to_private_ip_is_blocked(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(tmp_path, handler, resolver=lambda host: ["10.0.0.5"])
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "ssrf_blocked"


async def test_redirect_to_private_host_is_blocked(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "img.example.com":
            return httpx.Response(302, headers={"Location": "http://10.0.0.5/a.png"})
        raise AssertionError("redirect target must not be fetched")

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code in {"ssrf_blocked", "host_not_allowed"}


async def test_redirect_loop_is_bounded(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/again.png"})

    fetcher = make_fetcher(tmp_path, handler, max_redirects=2)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "too_many_redirects"


async def test_valid_redirect_is_followed(tmp_path: Path) -> None:
    body = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start.png":
            return httpx.Response(301, headers={"Location": "https://img.example.com/final.png"})
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        image = await fetcher.fetch("https://img.example.com/start.png")
    finally:
        await fetcher.aclose()

    assert image.final_url.endswith("/final.png")
    assert image.sha256


async def test_http_error_is_reported(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "http_404"


async def test_empty_allowlist_with_strict_mode_refuses_everything(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(
        tmp_path, handler, allowed_hosts=[], strict_allowlist=True
    )
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "host_not_allowed"


async def test_empty_allowlist_without_strict_mode_still_fetches(tmp_path: Path) -> None:
    body = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler, allowed_hosts=[], strict_allowlist=False)
    try:
        image = await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert image.size_bytes == len(body)


# -- strict_allowlist: default loose, SSRF guard always on ---------------------


async def test_public_host_outside_the_list_is_fetched_by_default(tmp_path: Path) -> None:
    """Nyaa posts link images on imagetwist/kr-av; a host list cannot cover them."""

    body = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)  # strict_allowlist defaults to False
    try:
        image = await fetcher.fetch("https://imagetwist.com/abc.jpg")
    finally:
        await fetcher.aclose()

    assert image.cdn_host == "imagetwist.com"
    assert image.path.exists()


@pytest.mark.parametrize(
    "address",
    ["10.0.0.5", "127.0.0.1", "169.254.169.254", "192.168.1.1", "::1", "fd00::1"],
)
async def test_private_addresses_are_still_rejected_in_loose_mode(
    tmp_path: Path, address: str
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(
        tmp_path,
        handler,
        strict_allowlist=False,
        resolver=lambda host: [address],
    )
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://anything.example.net/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "ssrf_blocked"


async def test_literal_metadata_ip_is_rejected_in_loose_mode(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(tmp_path, handler, strict_allowlist=False)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://169.254.169.254/latest/meta-data/")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "ssrf_blocked"


async def test_redirect_to_a_private_address_is_rejected_in_loose_mode(
    tmp_path: Path,
) -> None:
    """Every hop is re-validated, not just the first URL."""

    body = png_bytes()
    resolved: dict[str, list[str]] = {
        "public.example.net": [PUBLIC_IP],
        "internal.example.net": ["10.0.0.5"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example.net":
            return httpx.Response(302, headers={"location": "https://internal.example.net/x.png"})
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(
        tmp_path,
        handler,
        strict_allowlist=False,
        resolver=lambda host: resolved[host],
    )
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://public.example.net/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "ssrf_blocked"


async def test_loose_mode_still_rejects_bad_content_types_and_sizes(tmp_path: Path) -> None:
    def svg_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"<svg/>", headers={"content-type": "image/svg+xml"}
        )

    fetcher = make_fetcher(tmp_path, svg_handler, strict_allowlist=False)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://any.example.net/a.svg")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "bad_content_type"

    def huge_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=png_bytes(),
            headers={"content-type": "image/png", "content-length": "999999999"},
        )

    fetcher = make_fetcher(tmp_path, huge_handler, strict_allowlist=False)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://any.example.net/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "too_large"


async def test_loose_mode_still_rejects_a_spoofed_non_image(tmp_path: Path) -> None:
    """Content-Type says PNG but the bytes are not an image."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"not really a png", headers={"content-type": "image/png"}
        )

    fetcher = make_fetcher(tmp_path, handler, strict_allowlist=False)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://any.example.net/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "not_an_image"


# -- HTML "viewer" pages: resolve one level, then re-validate ------------------


VIEWER_HTML = """
<html><head><meta property="og:image" content="/media/real.jpg"></head>
<body><img src="/thumb.jpg" width="100" height="100"></body></html>
"""


async def test_viewer_page_resolves_og_image(tmp_path: Path) -> None:
    body = png_bytes()
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.path == "/page":
            return httpx.Response(
                200, content=VIEWER_HTML, headers={"content-type": "text/html"}
            )
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        image = await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()

    assert image.cdn_host == "img.example.com"
    assert image.size_bytes == len(body)
    assert requested == [
        "https://img.example.com/page",
        "https://img.example.com/media/real.jpg",
    ]


async def test_viewer_page_picks_the_largest_img(tmp_path: Path) -> None:
    html_page = """
    <html><body>
      <img src="/small.jpg" width="10" height="10">
      <img src="/big.jpg" width="800" height="600">
      <img src="/medium.jpg" width="300" height="200">
    </body></html>
    """
    body = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, content=html_page, headers={"content-type": "text/html"})
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        image = await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()
    assert image.final_url == "https://img.example.com/big.jpg"


async def test_viewer_page_without_images_gives_up(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content="<html><body>no pictures</body></html>",
            headers={"content-type": "text/html"},
        )

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "bad_content_type"


async def test_viewer_resolution_to_a_private_address_is_rejected(tmp_path: Path) -> None:
    html_page = '<html><head><meta property="og:image" content="https://internal.example.net/x.jpg"></head></html>'
    resolved = {
        "img.example.com": [PUBLIC_IP],
        "internal.example.net": ["10.0.0.5"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html_page, headers={"content-type": "text/html"})

    fetcher = make_fetcher(
        tmp_path, handler, resolver=lambda host: resolved.get(host, [PUBLIC_IP])
    )
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "ssrf_blocked"


async def test_viewer_resolution_follows_only_one_level(tmp_path: Path) -> None:
    """A second HTML page is not parsed again — no recursion."""

    nested = '<html><head><meta property="og:image" content="/deeper.jpg"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=nested, headers={"content-type": "text/html"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "bad_content_type"


async def test_viewer_resolution_is_off_by_default(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        return httpx.Response(200, content=VIEWER_HTML, headers={"content-type": "text/html"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/page")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "bad_content_type"


async def test_resolved_image_still_must_be_a_real_image(tmp_path: Path) -> None:
    html_page = '<html><head><meta property="og:image" content="/fake.png"></head></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, content=html_page, headers={"content-type": "text/html"})
        return httpx.Response(200, content=b"not an image", headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "not_an_image"


async def test_viewer_logs_are_emitted(tmp_path: Path) -> None:
    from structlog.testing import capture_logs

    body = png_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/page":
            return httpx.Response(200, content=VIEWER_HTML, headers={"content-type": "text/html"})
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    fetcher = make_fetcher(tmp_path, handler)
    try:
        with capture_logs() as logs:
            await fetcher.fetch("https://img.example.com/page", resolve_viewer=True)
    finally:
        await fetcher.aclose()

    events = [entry["event"] for entry in logs]
    assert "image.viewer_page_detected" in events
    assert "image.viewer_resolved" in events


def test_viewer_parser_prefers_og_image_over_links() -> None:
    from app.network.image_fetcher import image_url_from_viewer

    html_page = (
        '<html><head><meta property="og:image" content="https://cdn.example/og.jpg"></head>'
        '<body><a href="/other.png">x</a></body></html>'
    )
    assert image_url_from_viewer(html_page, "https://viewer.example/p/1") == "https://cdn.example/og.jpg"


def test_viewer_parser_uses_twitter_image() -> None:
    from app.network.image_fetcher import image_url_from_viewer

    html_page = '<html><head><meta name="twitter:image" content="/tw.jpg"></head></html>'
    assert image_url_from_viewer(html_page, "https://viewer.example/p/1") == "https://viewer.example/tw.jpg"


def test_viewer_parser_falls_back_to_image_links() -> None:
    from app.network.image_fetcher import image_url_from_viewer

    html_page = '<html><body><a href="/photo/full.png">full size</a></body></html>'
    assert image_url_from_viewer(html_page, "https://viewer.example/p/1") == "https://viewer.example/photo/full.png"
