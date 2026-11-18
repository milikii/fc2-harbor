"""Challenge / login-wall / empty-shell classification.

The cases mirror what the pilot sites actually return: a Cloudflare interstitial
(403 + cf-mitigated), a solved page that still carries the Cloudflare script, a
JavaScript-only search page with no result cards, and a login wall.
"""

from __future__ import annotations

from app.network.challenge import classify_page, looks_like_login_wall

CF_HTML = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div class="main-wrapper"><h1>Checking your browser before accessing
missav123.com</h1><p>Enable JavaScript and cookies to continue</p>
<script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
</div></body></html>"""

SOLVED_HTML = (
    """<!DOCTYPE html><html><head><title>FC2-PPV-4559944 結果</title></head>
<body><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
<div class="grid">"""
    + "".join(f'<div class="thumbnail"><a href="/en/{i}">FC2-PPV-{i}</a></div>' for i in range(5))
    + "</div></body></html>"
)

EMPTY_SHELL = "<html><head></head><body><div id=app></div></body></html>"

NO_RESULTS = """<html><head><title>検索結果 - サイト</title></head><body>
<header><nav><a href="/">ホーム</a><a href="/search">検索</a><a href="/login">ログイン</a>
<a href="/help">ヘルプ</a></nav></header>
<main><h1>該当する作品はありませんでした</h1>
<p>別のキーワードで検索してください。検索のヒントはヘルプをご覧ください。</p></main>
<footer>© 2026 example.com 無断転載を禁じます。プライバシーポリシー・利用規約・お問い合わせ</footer>
</body></html>"""

LOGIN_HTML = """<html><head><title>ログイン</title></head><body>
<form method="post" action="/login"><input name="email"><input type="password" name="password">
<button>ログイン</button></form></body></html>"""


def test_cloudflare_403_is_a_challenge() -> None:
    verdict = classify_page(
        status=403,
        headers={"cf-mitigated": "challenge", "server": "cloudflare"},
        html=CF_HTML,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "challenge"
    assert verdict.reason == "cf-mitigated"


def test_solved_page_with_cf_mitigated_header_is_not_a_challenge() -> None:
    """Byparr's solved page keeps both the header and the script; content decides."""

    verdict = classify_page(
        status=200,
        headers={"server": "cloudflare", "cf-mitigated": "challenge"},
        html=SOLVED_HTML,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"


def test_javascript_only_page_is_an_empty_shell() -> None:
    verdict = classify_page(
        status=200,
        headers=None,
        html=EMPTY_SHELL,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "empty_shell"
    assert verdict.is_challenge is True


def test_normal_no_results_page_is_not_a_challenge() -> None:
    verdict = classify_page(
        status=200,
        headers=None,
        html=NO_RESULTS,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"


def test_login_form_is_a_login_wall() -> None:
    assert looks_like_login_wall(LOGIN_HTML) is True
    verdict = classify_page(
        status=200,
        headers=None,
        html=LOGIN_HTML,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "login_wall"


def test_navigation_login_link_alone_is_not_a_wall() -> None:
    assert looks_like_login_wall(NO_RESULTS) is False


def test_hidden_login_modal_is_not_a_wall() -> None:
    """missav ships its login/register modal in every page, hidden by markup.

    The static HTML still carries the password inputs; only a visible form counts.
    """

    html = """<html><head><title>検索結果 - サイト</title></head><body>
<div x-show="showModal.login" style="display: none;">
<form @submit.prevent="login"><input type="password" name="password"></form>
</div>
<div class="thumbnail"><a href="/en/1">FC2-PPV-1234567 タイトル</a></div>
</body></html>"""

    assert looks_like_login_wall(html) is False
    verdict = classify_page(
        status=200,
        headers={"cf-mitigated": "challenge"},
        html=html,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"


def test_required_content_wins_over_a_visible_login_form() -> None:
    """A provider that can still parse its cards must not be called unavailable."""

    html = """<html><body><form action="/login"><input type="password"></form>
<div class="thumbnail">FC2-PPV-1234567 タイトル</div></body></html>"""

    verdict = classify_page(
        status=200,
        headers=None,
        html=html,
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"


def test_404_page_is_not_a_challenge() -> None:
    verdict = classify_page(
        status=404,
        headers=None,
        html="<html><body><h1>Not Found</h1><p>The page does not exist.</p></body></html>",
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"
