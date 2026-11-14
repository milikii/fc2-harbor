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
        ("https://127.0.0.1/a.png", "host_not_allowed"),
        ("https://localhost/a.png", "ssrf_blocked"),
        ("file:///etc/passwd", "bad_scheme"),
        ("https://evil.example.net/a.png", "host_not_allowed"),
    ],
)
async def test_url_guards(tmp_path: Path, url: str, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(tmp_path, handler)
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


async def test_empty_allowlist_refuses_everything(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request must be made")

    fetcher = make_fetcher(tmp_path, handler, allowed_hosts=[])
    try:
        with pytest.raises(ImageFetchError) as excinfo:
            await fetcher.fetch("https://img.example.com/a.png")
    finally:
        await fetcher.aclose()
    assert excinfo.value.code == "no_allowlist"
