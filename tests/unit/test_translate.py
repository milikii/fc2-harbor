"""Translation client — spec 18 (translation section).

Mocked transport only: no API key, no network. Failure modes matter more than the
happy path, because a translation error must never fail the card.
"""

from __future__ import annotations

import httpx
import pytest
from app.services.translate import OpenAITranslator, TranslationError


def translator(handler, **kwargs: object) -> OpenAITranslator:
    options: dict[str, object] = {
        "base_url": "https://translate.example/v1",
        "api_key": "test-key",
        "model": "test-model",
        "timeout_seconds": 5,
        "transport": httpx.MockTransport(handler),
    }
    options.update(kwargs)
    return OpenAITranslator(**options)  # type: ignore[arg-type]


async def test_translation_returns_chinese_text() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"choices": [{"message": {"content": "  中文译文  "}}]})

    client = translator(handler)
    try:
        result = await client.translate("日本語の説明")
    finally:
        await client.aclose()

    assert result == "中文译文"
    assert captured["path"] == "/v1/chat/completions"
    assert captured["auth"] == "Bearer test-key"


async def test_empty_input_skips_the_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("no request for empty input")

    client = translator(handler)
    try:
        assert await client.translate("") is None
        assert await client.translate(None) is None
    finally:
        await client.aclose()


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "http_400"), (401, "http_401"), (500, "http_500")],
)
async def test_http_errors_are_typed(status: int, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": "nope"})

    client = translator(handler)
    try:
        with pytest.raises(TranslationError) as excinfo:
            await client.translate("日本語")
    finally:
        await client.aclose()
    assert excinfo.value.code == code


async def test_bad_response_shape_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": True})

    client = translator(handler)
    try:
        with pytest.raises(TranslationError) as excinfo:
            await client.translate("日本語")
    finally:
        await client.aclose()
    assert excinfo.value.code == "bad_response"


async def test_empty_translation_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "   "}}]})

    client = translator(handler)
    try:
        with pytest.raises(TranslationError) as excinfo:
            await client.translate("日本語")
    finally:
        await client.aclose()
    assert excinfo.value.code == "empty"


async def test_network_error_is_typed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = translator(handler)
    try:
        with pytest.raises(TranslationError) as excinfo:
            await client.translate("日本語")
    finally:
        await client.aclose()
    assert excinfo.value.code == "network"


async def test_long_input_is_truncated_before_sending() -> None:
    sent: dict[str, int] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.content.decode()
        sent["chars"] = len(body)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = translator(handler)
    try:
        await client.translate("あ" * 9000)
    finally:
        await client.aclose()

    assert sent["chars"] < 9000 * 3  # the payload did not grow with the input
