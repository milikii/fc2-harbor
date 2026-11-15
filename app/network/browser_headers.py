"""Browser-like request headers for provider fetches — anti-bot defence.

The old system hit javten (``fc2_hub``) on a quarter of all FC2 numbers; today the
same site answers 403. The data is still there — the request simply looks like a
script: one ``Referer`` header, no ``Accept``, no ``Accept-Language``, no fetch
metadata, and no cookie from a first page visit.

This module builds a header set that matches a real Chrome navigation, per host,
with configuration overrides. It deliberately does **not** try to fake the TLS
fingerprint (JA3): that needs another HTTP stack and is a separate decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlsplit

#: A current desktop Chrome UA. Kept in one place so all providers agree.
CHROME_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

#: Headers a Chrome navigation always sends. ``Accept-Encoding`` lists only the
#: codings httpx decodes without extra dependencies (no ``br``/``zstd``).
BASE_HEADERS: dict[str, str] = {
    "User-Agent": CHROME_USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Upgrade-Insecure-Requests": "1",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}


def provider_headers(
    host: str,
    *,
    referer: str | None = None,
    overrides: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """A Chrome-like header set for ``host``, with ``Sec-Fetch-*`` derived.

    ``Sec-Fetch-Site`` is what a browser would report for this navigation: a
    same-origin referer means ``same-origin``, a different host means
    ``cross-site``, and no referer (typed URL) means ``none``.
    """

    headers = dict(BASE_HEADERS)
    if referer:
        headers["Referer"] = referer
        referer_host = (urlsplit(referer).hostname or "").lower()
        headers["Sec-Fetch-Site"] = (
            "same-origin" if referer_host == host.lower() else "cross-site"
        )
    else:
        headers["Sec-Fetch-Site"] = "none"
    headers["Sec-Fetch-Mode"] = "navigate"
    headers["Sec-Fetch-User"] = "?1"
    headers["Sec-Fetch-Dest"] = "document"
    if overrides:
        headers.update({str(key): str(value) for key, value in overrides.items()})
    return headers
