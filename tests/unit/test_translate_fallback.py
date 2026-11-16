"""MyMemory fallback and the 400-character truncation.

Mocked transport only. Google rate-limits the NAS exit IP, so the fallback is what
keeps Chinese text on the cards.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.services.translate import (
    TRANSLATE_MAX_CHARS,
    FallbackTranslator,
    GoogleFreeTranslator,
    MyMemoryTranslator,
    TranslationError,
)


async def _no_sleep(_seconds: float) -> None:
    return None


def mymemory(handler: Any, **kwargs: Any) -> MyMemoryTranslator:
    options: dict[str, Any] = {
        "target_language": "zh-CN",
        "source_language": "ja",
        "transport": httpx.MockTransport(handler),
        "timeout_seconds": 5,
    }
    options.update(kwargs)
    return MyMemoryTranslator(**options)


def google(handler: Any) -> GoogleFreeTranslator:
    return GoogleFreeTranslator(
        target_language="zh-CN",
        source_language="ja",
        transport=httpx.MockTransport(handler),
        timeout_seconds=5,
        sleep=_no_sleep,
    )


def ok(text: str) -> httpx.Response:
    return httpx.Response(
        200, json={"responseData": {"translatedText": text}, "responseStatus": 200}
    )


async def test_mymemory_returns_the_translation() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("18 岁的新人少女首次拍摄。")

    translator = mymemory(handler)
    translated = await translator.translate("18歳の新人少女が初撮影。")

    assert translated == "18 岁的新人少女首次拍摄。"
    assert requests[0].url.params["langpair"] == "ja|zh-CN"
    assert requests[0].url.params["q"] == "18歳の新人少女が初撮影。"
    await translator.aclose()


async def test_mymemory_splits_long_text() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return ok("译文")

    translator = mymemory(handler)
    text = "あ" * 1200
    translated = await translator.translate(text)

    assert len(requests) > 1
    assert all(len(r.url.params["q"]) <= 500 for r in requests)
    assert translated == "译文" * len(requests)
    await translator.aclose()


async def test_mymemory_rejects_a_bad_payload() -> None:
    translator = mymemory(lambda r: httpx.Response(200, json={"responseData": {}}))
    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("説明")
    assert excinfo.value.code == "bad_response"
    await translator.aclose()


async def test_mymemory_http_error_is_typed() -> None:
    translator = mymemory(lambda r: httpx.Response(429))
    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("説明")
    assert excinfo.value.code == "http_429"
    await translator.aclose()


# -- the chain ----------------------------------------------------------------


async def test_google_failure_falls_back_to_mymemory() -> None:
    def google_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429)

    def mymemory_handler(request: httpx.Request) -> httpx.Response:
        return ok("回退译文")

    chain = FallbackTranslator(
        primary=google(google_handler), fallback=mymemory(mymemory_handler)
    )
    assert await chain.translate("説明文です。") == "回退译文"


async def test_both_failing_returns_none() -> None:
    chain = FallbackTranslator(
        primary=google(lambda r: httpx.Response(429)),
        fallback=mymemory(lambda r: httpx.Response(500)),
    )
    assert await chain.translate("説明文です。") is None


async def test_without_a_fallback_the_error_propagates() -> None:
    chain = FallbackTranslator(primary=google(lambda r: httpx.Response(429)))
    with pytest.raises(TranslationError):
        await chain.translate("説明文です。")


async def test_only_the_first_four_hundred_characters_are_translated() -> None:
    seen: list[str] = []

    def google_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["q"])
        return httpx.Response(200, json=["译文"])

    chain = FallbackTranslator(primary=google(google_handler))
    await chain.translate("あ" * 1673)

    assert len(seen) == 1
    assert len(seen[0]) == TRANSLATE_MAX_CHARS == 400


async def test_empty_input_is_not_sent_anywhere() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return ok("x")

    chain = FallbackTranslator(primary=google(handler), fallback=mymemory(handler))
    assert await chain.translate(None) is None
    assert await chain.translate("   ") is None
    assert calls == []


async def test_a_chinese_skip_does_not_trigger_the_fallback() -> None:
    """Google returning None means "nothing to translate", not "failed"."""

    fallback_calls: list[str] = []

    def mymemory_handler(request: httpx.Request) -> httpx.Response:
        fallback_calls.append(str(request.url))
        return ok("不应调用")

    chain = FallbackTranslator(
        primary=google(lambda r: httpx.Response(200, json=["译文"])),
        fallback=mymemory(mymemory_handler),
    )
    # Chinese input is skipped by the primary (None), which is final.
    assert await chain.translate("这是已经写好的中文简介。") is None
    assert fallback_calls == []
