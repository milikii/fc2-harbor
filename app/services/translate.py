"""Japanese → Chinese description translation — spec 8 / 18.

Two backends:

* ``google_free`` — the key-less ``clients5.google.com/translate_a/t`` endpoint
  (``client=dict-chrome-ex``). It answers a JSON array, one element per ``q``
  parameter, so several chunks travel in a single GET. The response is validated:
  an HTML interstitial ("Sorry...") is a **failure**, never a translation.
* ``openai_compatible`` — a chat-completions endpoint.

Failures are never fatal: a missing translation only means the card has no Chinese
text, the Japanese one is still there (spec 9.1 requires both, but never a silently
truncated body). Translations are cached in ``contents.description_zh``.

Only idempotent GETs are retried (spec 8.3); the OpenAI POST is not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from typing import Any, Protocol

import httpx

from app.logging import get_logger

_log = get_logger("translate")

#: Key-less Google endpoint. Verified working through the configured proxy; the
#: ``translate.googleapis.com/.../single?client=gtx`` variant is blocked from the
#: same exit, so it is deliberately not used.
GOOGLE_FREE_ENDPOINT = "https://clients5.google.com/translate_a/t"
GOOGLE_FREE_CLIENT = "dict-chrome-ex"

#: URL length is finite: translate in sentence-aligned blocks, several per request.
CHUNK_TARGET_CHARS = 900
CHUNK_MAX_CHARS = 1000
#: Politeness: at most one request per second (spec 8.3 rate limiting).
MIN_INTERVAL_SECONDS = 1.0
RETRY_ATTEMPTS = 2
RETRYABLE_HTTP_CODES = frozenset({"http_429", "http_500", "http_502", "http_503", "http_504"})
RETRY_BACKOFF_SECONDS = 1.0

MAX_INPUT_CHARS = 4000
SYSTEM_PROMPT = (
    "你是专业的日文到简体中文翻译。把用户给出的日文作品简介翻译成简体中文："
    "保留人名、番号、专有名词，不添加原文没有的内容，不解释，只输出译文。"
)


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Translator(Protocol):
    """Anything the metadata service can call to translate a description."""

    async def translate(self, text: str | None) -> str | None: ...

    async def aclose(self) -> None: ...


def looks_chinese(text: str) -> bool:
    """True when the text is Chinese already (CJK present, no kana at all)."""

    has_cjk = False
    for char in text:
        code = ord(char)
        if 0x3040 <= code <= 0x30FF:  # hiragana / katakana → definitely Japanese
            return False
        if 0x4E00 <= code <= 0x9FFF:
            has_cjk = True
    return has_cjk


def split_for_translation(
    text: str, *, target: int = CHUNK_TARGET_CHARS, hard_max: int = CHUNK_MAX_CHARS
) -> list[str]:
    """Split into sentence-aligned chunks, preserving every character in order."""

    remaining = text.strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while len(remaining) > hard_max:
        window = remaining[:hard_max]
        cut = max(
            window.rfind("。"),
            window.rfind("！"),
            window.rfind("？"),
            window.rfind("\n"),
            window.rfind(". "),
        )
        if cut < target // 2:
            cut = window.rfind("、")
        if cut < target // 2:
            cut = hard_max
        else:
            cut += 1  # keep the sentence terminator with its sentence
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining.strip())
    return [chunk for chunk in chunks if chunk]


def _google_items(payload: Any) -> list[str] | None:
    """Read the flat or the nested shape of ``translate_a/t``, or ``None``."""

    if not isinstance(payload, list) or not payload:
        return None
    texts: list[str] = []
    for item in payload:
        if isinstance(item, str):
            texts.append(item)
        elif isinstance(item, list) and item and isinstance(item[0], str):
            texts.append(item[0])
        else:
            return None
    return texts


def _content_text(content: object) -> str:
    """Normalise ``message.content``.

    Older endpoints return a plain string; newer OpenAI-compatible ones may return a
    list of parts (``[{"type": "text", "text": "..."}]``). Both are accepted instead
    of assuming one version's shape.
    """

    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


class OpenAITranslator:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        target_language: str = "zh-CN",
        timeout_seconds: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._target_language = target_language
        self._timeout = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                # Spec 8.2: never inherit HTTP_PROXY for an explicit endpoint.
                trust_env=False,
                transport=self._transport,
                headers={"Authorization": f"Bearer {self._api_key}"},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def translate(self, text: str | None) -> str | None:
        if not text or not text.strip():
            return None
        payload = {
            "model": self._model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"目标语言：{self._target_language}\n\n原文：\n{text[:MAX_INPUT_CHARS]}"
                    ),
                },
            ],
        }
        started = time.monotonic()
        try:
            response = await self.client.post(f"{self._base_url}/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise TranslationError(
                "network", f"translation request failed: {type(exc).__name__}"
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            raise TranslationError(
                f"http_{response.status_code}",
                f"translation endpoint answered {response.status_code}",
            )
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError("bad_response", "translation response is not usable") from exc

        translated = _content_text(content).strip()
        if not translated:
            raise TranslationError("empty", "translation returned no text")
        _log.info("translate.done", latency_ms=latency_ms, chars=len(translated))
        return translated


class GoogleFreeTranslator:
    """Key-less Google translate over GET, with chunking, batching and validation.

    The endpoint answers a JSON array with one entry per ``q`` parameter. Anything
    that is not such an array (an HTML "Sorry..." interstitial, an empty array, a
    length mismatch) is a failure: a blocked exit must never look like a
    translation.
    """

    def __init__(
        self,
        *,
        target_language: str = "zh-CN",
        source_language: str = "ja",
        proxy_url: str | None = None,
        endpoint: str = GOOGLE_FREE_ENDPOINT,
        timeout_seconds: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._target_language = target_language
        self._source_language = source_language
        self._proxy_url = proxy_url
        self._endpoint = endpoint
        self._timeout = timeout_seconds
        self._transport = transport
        self._clock = clock
        self._sleep = sleep
        self._last_call = 0.0
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                # Spec 8.2: the proxy comes from the profile, never from the env.
                trust_env=False,
                transport=self._transport,
                proxy=self._proxy_url or None,
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def translate(self, text: str | None) -> str | None:
        if not text or not text.strip():
            return None
        if looks_chinese(text):
            _log.info("translate.skipped_chinese", chars=len(text))
            return None
        chunks = split_for_translation(text)
        if not chunks:
            return None
        started = time.monotonic()
        translations = await self._request_with_retry(chunks)
        translated = "".join(translations).strip()
        if not translated:
            raise TranslationError("empty", "translation returned no text")
        _log.info(
            "translate.done",
            latency_ms=int((time.monotonic() - started) * 1000),
            chunks=len(chunks),
            chars=len(translated),
            provider="google_free",
        )
        return translated

    # -- HTTP ---------------------------------------------------------------

    async def _throttle(self) -> None:
        elapsed = self._clock() - self._last_call
        if self._last_call and elapsed < MIN_INTERVAL_SECONDS:
            await self._sleep(MIN_INTERVAL_SECONDS - elapsed)

    async def _request(self, chunks: Sequence[str]) -> list[str]:
        await self._throttle()
        params: list[tuple[str, str | int | float | bool | None]] = [
            ("client", GOOGLE_FREE_CLIENT),
            ("sl", self._source_language),
            ("tl", self._target_language),
            *(("q", chunk) for chunk in chunks),
        ]
        try:
            response = await self.client.get(self._endpoint, params=params)
        except httpx.HTTPError as exc:
            raise TranslationError(
                "network", f"translation request failed: {type(exc).__name__}"
            ) from exc
        finally:
            self._last_call = self._clock()

        if response.status_code >= 400:
            raise TranslationError(
                f"http_{response.status_code}",
                f"translation endpoint answered {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            # HTML ("Sorry...") or anything else that is not JSON.
            raise TranslationError(
                "non_json",
                f"translation response is not JSON (bytes={len(response.content)})",
            ) from exc

        texts = _google_items(payload)
        if texts is None:
            raise TranslationError(
                "bad_response", f"translation response has an unexpected shape: {type(payload)}"
            )
        if len(texts) != len(chunks):
            raise TranslationError(
                "length_mismatch",
                f"translation returned {len(texts)} texts for {len(chunks)} chunks",
            )
        return texts

    async def _request_with_retry(self, chunks: Sequence[str]) -> list[str]:
        last: TranslationError | None = None
        for attempt in range(RETRY_ATTEMPTS + 1):
            try:
                return await self._request(chunks)
            except TranslationError as exc:
                last = exc
                retryable = exc.code == "network" or exc.code in RETRYABLE_HTTP_CODES
                if not retryable or attempt == RETRY_ATTEMPTS:
                    raise
                delay = RETRY_BACKOFF_SECONDS * (2**attempt)
                _log.warning(
                    "translate.retry",
                    attempt=attempt + 1,
                    error_code=exc.code,
                    delay_seconds=delay,
                )
                await self._sleep(delay)
        # pragma: no cover - the loop always returns or raises above
        raise last if last is not None else TranslationError("unknown", "translation failed")
