"""Cloudflare challenge, login wall and empty-shell detection.

The HTTP client sees what a script sees: Cloudflare answers a JavaScript
challenge with ``403``/``503``, a ``cf-mitigated: challenge`` header and an
interstitial whose only content is "Just a moment...". A solved page may still
carry the Cloudflare ``challenge-platform`` script, so a marker alone is not
enough: the page is only a challenge when the content the adapter needs is
absent.

The same module recognises a login wall. A login wall is **not** a challenge:
Byparr cannot (and must not) log in, so the provider is marked unavailable
instead of asking the solver.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from selectolax.lexbor import LexborHTMLParser

#: Phrases that only appear on a Cloudflare / anti-bot interstitial. Matched
#: case-insensitively against the whole document.
CHALLENGE_MARKERS: tuple[str, ...] = (
    "just a moment",
    "checking your browser",
    "checking if the site connection is secure",
    "cf-browser-verification",
    "cf_chl_opt",
    "challenge-platform",
    "attention required! | cloudflare",
    "ddos protection by cloudflare",
    "enable javascript and cookies to continue",
    "verifying you are human",
    "verify you are human",
)

#: Login-wall wording. A page that has a password field is a login wall even
#: without any of these strings; the strings only help when the form is rendered
#: by JavaScript and the password input is not in the static HTML.
LOGIN_MARKERS: tuple[str, ...] = (
    "sign in",
    "signin",
    "log in",
    "login",
    "登录",
    "登入",
    "会员登录",
    "會員登入",
)

#: Below this much visible text a page with no required element is an empty shell.
EMPTY_TEXT_CHARS = 200
#: A 403/503 with this little text is an interstitial even without a marker.
CHALLENGE_TEXT_CHARS = 400

PageKind = Literal["ok", "challenge", "login_wall", "empty_shell"]


@dataclass(frozen=True, slots=True)
class PageVerdict:
    kind: PageKind
    reason: str | None = None

    @property
    def is_challenge(self) -> bool:
        return self.kind in {"challenge", "empty_shell"}


def _visible_text(tree: LexborHTMLParser) -> str:
    body = tree.body
    if body is None:
        return ""
    return " ".join(body.text(separator=" ", strip=True).split())


def _header(headers: Mapping[str, str] | None, name: str) -> str | None:
    if not headers:
        return None
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def looks_like_login_wall(html: str) -> bool:
    """True for a page that asks for credentials.

    Deliberately conservative: a password input, or a form whose action points at
    a login endpoint, is decisive. Wording alone is not, because every provider
    links to a login page in its navigation.
    """

    if not html:
        return False
    tree = LexborHTMLParser(html)
    if tree.css_first('input[type="password"]') is not None:
        return True
    for form in tree.css("form"):
        action = (form.attributes.get("action") or "").lower()
        if "login" in action or "signin" in action or "sign-in" in action:
            return True
    title = tree.css_first("title")
    title_text = (title.text(strip=True) if title is not None else "").lower()
    return bool(title_text) and any(marker in title_text for marker in LOGIN_MARKERS)


def classify_page(
    *,
    status: int,
    headers: Mapping[str, str] | None,
    html: str,
    required_selectors: Iterable[str] = (),
) -> PageVerdict:
    """Classify one response as ok / challenge / login wall / empty shell.

    ``required_selectors`` are the elements the adapter cannot work without (its
    result cards). A page that contains one of them is never called a challenge,
    even if Cloudflare's script tag is still in the HTML.
    """

    if not html.strip():
        return PageVerdict("challenge", "empty_body")

    mitigated = _header(headers, "cf-mitigated")
    if mitigated and "challenge" in mitigated.lower():
        return PageVerdict("challenge", "cf-mitigated")

    tree = LexborHTMLParser(html)
    selectors = [selector for selector in required_selectors if selector]
    has_required = any(tree.css_first(selector) is not None for selector in selectors)

    if looks_like_login_wall(html):
        return PageVerdict("login_wall", "credentials_form")

    lowered = html.lower()
    marker = next((item for item in CHALLENGE_MARKERS if item in lowered), None)
    text_length = len(_visible_text(tree))

    if status in {403, 503}:
        if marker:
            return PageVerdict("challenge", f"http_{status}+{marker}")
        if not has_required and text_length < CHALLENGE_TEXT_CHARS:
            return PageVerdict("challenge", f"http_{status}+empty")

    if marker and not has_required and text_length < CHALLENGE_TEXT_CHARS:
        return PageVerdict("challenge", f"marker+empty:{marker}")

    # Only a successful-looking page can be an empty shell. A 404/500 page is an
    # answer, not a bot wall, and must stay a normal miss/error.
    if (
        status < 400
        and selectors
        and not has_required
        and text_length < EMPTY_TEXT_CHARS
    ):
        title = tree.css_first("title")
        if title is None or not title.text(strip=True):
            return PageVerdict("empty_shell", "no_required_element")

    return PageVerdict("ok")
