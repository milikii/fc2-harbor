"""Japanese → Chinese description translation — spec 8 / 18.

Uses an OpenAI-compatible chat-completions endpoint. Failures are never fatal: a
missing translation only means the card has no Chinese text, the Japanese one is
still there (spec 9.1 requires both, but never a silently truncated body).

The POST is deliberately **not** retried automatically (spec 8.3 retries only
idempotent GETs); the caller may retry later because translations are cached in
``contents.description_zh``.
"""

from __future__ import annotations

import time

import httpx

from app.logging import get_logger

_log = get_logger("translate")

MAX_INPUT_CHARS = 4000
SYSTEM_PROMPT = (
    "你是专业的日文到简体中文翻译。把用户给出的日文作品简介翻译成简体中文："
    "保留人名、番号、专有名词，不添加原文没有的内容，不解释，只输出译文。"
)


class TranslationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


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
