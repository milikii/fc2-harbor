"""Key-less Google translation — chunking, batching, response validation.

Mocked transport only. The important property is that a blocked or broken endpoint
(a "Sorry..." HTML page, an empty array, a length mismatch) is a **failure**, never
a translation that silently reaches a card.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from app.services.translate import (
    CHUNK_MAX_CHARS,
    MIN_INTERVAL_SECONDS,
    GoogleFreeTranslator,
    TranslationError,
    looks_chinese,
    split_for_translation,
)


class Recorder:
    """Transport double: records requests and answers with queued responses."""

    def __init__(self, *responses: httpx.Response | Exception) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def qs(self, index: int = -1) -> list[str]:
        return self.requests[index].url.params.get_list("q")


def make(recorder: Recorder, **kwargs: Any) -> GoogleFreeTranslator:
    options: dict[str, Any] = {
        "target_language": "zh-CN",
        "source_language": "ja",
        "transport": httpx.MockTransport(recorder),
        "sleep": _no_sleep,
    }
    options.update(kwargs)
    return GoogleFreeTranslator(**options)


async def _no_sleep(_seconds: float) -> None:
    return None


def json_response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def html_response(status: int = 200) -> httpx.Response:
    return httpx.Response(
        status,
        text="<html><body>Sorry...</body></html>",
        headers={"content-type": "text/html; charset=UTF-8"},
    )


async def test_single_text_is_translated() -> None:
    recorder = Recorder(json_response(["素人首拍 主要部分脸部展示"]))
    translator = make(recorder)

    assert await translator.translate("素人初撮り・本編顔出し") == "素人首拍 主要部分脸部展示"
    request = recorder.requests[0]
    assert request.url.params["client"] == "dict-chrome-ex"
    assert request.url.params["sl"] == "ja"
    assert request.url.params["tl"] == "zh-CN"
    assert recorder.qs() == ["素人初撮り・本編顔出し"]


async def test_empty_input_skips_the_request() -> None:
    recorder = Recorder()
    translator = make(recorder)

    assert await translator.translate(None) is None
    assert await translator.translate("   ") is None
    assert recorder.requests == []


async def test_chinese_input_is_not_translated() -> None:
    recorder = Recorder()
    translator = make(recorder)

    assert await translator.translate("这是已经中文写好的简介。") is None
    assert recorder.requests == []


def test_looks_chinese_distinguishes_kana() -> None:
    assert looks_chinese("这是中文") is True
    assert looks_chinese("これは日本語です") is False
    assert looks_chinese("FC2-PPV-123") is False


async def test_long_text_is_batched_in_one_request() -> None:
    chunks = split_for_translation("あ" * 2500)
    assert len(chunks) > 1
    assert all(len(chunk) <= CHUNK_MAX_CHARS for chunk in chunks)

    recorder = Recorder(json_response([f"译文{index}" for index in range(len(chunks))]))
    translator = make(recorder)

    translated = await translator.translate("あ" * 2500)
    assert len(recorder.requests) == 1
    assert recorder.qs() == chunks
    assert translated == "".join(f"译文{index}" for index in range(len(chunks)))


def test_chunking_preserves_every_character_in_order() -> None:
    text = "。".join("段落" * 100 for _ in range(8))
    chunks = split_for_translation(text)
    assert "".join(chunks) == text.replace("\n", "")


def test_chunking_prefers_sentence_boundaries() -> None:
    text = ("あ" * 400 + "。") * 5
    chunks = split_for_translation(text)
    assert all(chunk.endswith("。") for chunk in chunks[:-1])


async def test_html_sorry_page_is_a_failure() -> None:
    recorder = Recorder(html_response())
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("これは説明です")
    assert excinfo.value.code == "non_json"


async def test_empty_array_is_a_failure() -> None:
    recorder = Recorder(json_response([]))
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("これは説明です")
    assert excinfo.value.code == "bad_response"


async def test_length_mismatch_is_a_failure() -> None:
    chunks = split_for_translation("あ" * 2500)
    assert len(chunks) > 1
    recorder = Recorder(json_response(["只有一个译文"]))
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("あ" * 2500)
    assert excinfo.value.code == "length_mismatch"


async def test_non_list_payload_is_a_failure() -> None:
    recorder = Recorder(json_response({"translation": "译文"}))
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("これは説明です")
    assert excinfo.value.code == "bad_response"


async def test_nested_shape_is_accepted() -> None:
    recorder = Recorder(json_response([["译文", "原文"]]))
    translator = make(recorder)

    assert await translator.translate("これは説明です") == "译文"


async def test_http_error_is_typed() -> None:
    recorder = Recorder(httpx.Response(400))
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("これは説明です")
    assert excinfo.value.code == "http_400"
    assert len(recorder.requests) == 1


async def test_rate_limit_is_retried() -> None:
    recorder = Recorder(httpx.Response(429), json_response(["译文"]))
    translator = make(recorder)

    assert await translator.translate("これは説明です") == "译文"
    assert len(recorder.requests) == 2


async def test_network_error_is_retried_then_succeeds() -> None:
    recorder = Recorder(httpx.ConnectTimeout("boom"), json_response(["译文"]))
    translator = make(recorder)

    assert await translator.translate("これは説明です") == "译文"
    assert len(recorder.requests) == 2


async def test_network_error_gives_up_after_two_retries() -> None:
    recorder = Recorder(
        httpx.ConnectTimeout("boom"),
        httpx.ConnectTimeout("boom"),
        httpx.ConnectTimeout("boom"),
    )
    translator = make(recorder)

    with pytest.raises(TranslationError) as excinfo:
        await translator.translate("これは説明です")
    assert excinfo.value.code == "network"
    assert len(recorder.requests) == 3


async def test_bad_response_is_not_retried() -> None:
    recorder = Recorder(html_response())
    translator = make(recorder)

    with pytest.raises(TranslationError):
        await translator.translate("これは説明です")
    assert len(recorder.requests) == 1


async def test_requests_are_rate_limited() -> None:
    sleeps: list[float] = []

    class FakeClock:
        def __init__(self) -> None:
            self.now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = FakeClock()

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.now += seconds

    recorder = Recorder(json_response(["一"]), json_response(["二"]))
    translator = make(recorder, sleep=record_sleep, clock=clock)

    await translator.translate("これは説明一です")
    await translator.translate("これは説明二です")

    # One second between requests, no more.
    assert sleeps == [MIN_INTERVAL_SECONDS]


async def test_proxy_is_used_for_the_client(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    translator = GoogleFreeTranslator(proxy_url="http://192.0.2.10:7890")
    _ = translator.client

    assert captured["proxy"] == "http://192.0.2.10:7890"
    # Spec 8.2: the profile decides the proxy, never the environment.
    assert captured["trust_env"] is False
    await translator.aclose()


async def test_direct_profile_means_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    real_client = httpx.AsyncClient

    def spy(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        captured.update(kwargs)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy)
    translator = GoogleFreeTranslator(proxy_url=None)
    _ = translator.client

    assert captured["proxy"] is None
    await translator.aclose()


async def test_client_is_closed() -> None:
    translator = make(Recorder())
    _ = translator.client
    await translator.aclose()
    assert translator._client is None
