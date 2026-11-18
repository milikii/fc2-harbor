"""Provider fallback order: HTTP → Byparr → provider error.

The provider is faked, the HTTP client is faked, and the Byparr client is faked:
this pins the decision logic (when to ask the solver, when never to ask it)
without a browser or a network.
"""

from __future__ import annotations

from typing import Any

import pytest
from app.db.enums import ContentType
from app.db.models import Content
from app.network.challenge import classify_page
from app.network.clients import HttpError, HttpResponse
from app.network.flaresolverr import FlareSolverrError, FlareSolverrResult
from app.providers.base import BaseProvider, ProviderError, ProviderPage
from app.schemas.metadata import MetadataResult, ProviderContext

URL = "https://missav123.com/search/FC2-PPV-4559944"
SOLVED = (
    '<html><head><title>FC2-PPV-4559944 結果</title></head><body>'
    '<div class="thumbnail">FC2-PPV-4559944 タイトル</div></body></html>'
)
CHALLENGE = (
    '<html><head><title>Just a moment...</title></head><body>'
    "Checking your browser before accessing</body></html>"
)
LOGIN = '<html><body><form action="/login"><input type="password"></form></body></html>'


def make_content() -> Content:
    return Content(
        content_type=ContentType.FC2,
        canonical_key="fc2_ppv:4559944",
        display_code="FC2-PPV-4559944",
    )


def ctx() -> ProviderContext:
    return ProviderContext(
        canonical_key="fc2_ppv:4559944",
        display_code="FC2-PPV-4559944",
        proxy_profile="japan",
    )


class FakeHttp:
    def __init__(
        self,
        *,
        response: HttpResponse | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls = 0

    async def get(self, url: str, **kwargs: Any) -> HttpResponse:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


class FakeSolver:
    budget_seconds = 100.0

    def __init__(
        self,
        *,
        result: FlareSolverrResult | None = None,
        error: FlareSolverrError | None = None,
    ) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def request_get(
        self,
        url: str,
        *,
        proxy_url: str | None,
        required_selectors: tuple[str, ...] = (),
    ) -> FlareSolverrResult:
        self.calls.append(
            {
                "url": url,
                "proxy_url": proxy_url,
                "required_selectors": required_selectors,
            }
        )
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result


class DemoProvider(BaseProvider):
    id = "miss_av"
    host = "missav123.com"
    proxy_key = "miss_av"
    CARD = ".thumbnail"
    REQUIRED_SELECTORS = (CARD,)

    def build_url(self, content: Content) -> str:
        return URL

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        if "FC2-PPV-4559944" not in page.html:
            return None
        return self._result(
            content,
            title="FC2-PPV-4559944 タイトル",
            description="作品の説明です。",
            official_url=page.url,
        )


def solved_result(*, status: int = 200, html: str = SOLVED) -> FlareSolverrResult:
    return FlareSolverrResult(
        url=URL,
        status=status,
        html=html,
        cookie_names=("cf_clearance",),
        user_agent="Mozilla/5.0",
        latency_ms=12,
        attempts=1,
    )


async def run(
    *,
    http: FakeHttp,
    solver: FakeSolver | None,
    proxy_url: str | None = "http://127.0.0.1:7890",
) -> MetadataResult:
    provider = DemoProvider(
        client=http,  # type: ignore[arg-type]
        proxy_url=proxy_url,
        timeout_seconds=5,
        flaresolverr=solver,  # type: ignore[arg-type]
    )
    return await provider.fetch(make_content(), ctx())


async def test_http_403_falls_back_to_byparr() -> None:
    http = FakeHttp(error=HttpError("forbidden", status=403))
    solver = FakeSolver(result=solved_result())
    result = await run(http=http, solver=solver)

    assert result.title_ja == "FC2-PPV-4559944 タイトル"
    assert len(solver.calls) == 1
    assert solver.calls[0]["proxy_url"] == "http://127.0.0.1:7890"


async def test_http_challenge_page_falls_back_to_byparr() -> None:
    http = FakeHttp(
        response=HttpResponse(
            url=URL,
            status=200,
            content=CHALLENGE.encode(),
            headers={"cf-mitigated": "challenge"},
        )
    )
    solver = FakeSolver(result=solved_result())
    result = await run(http=http, solver=solver)

    assert result.title_ja == "FC2-PPV-4559944 タイトル"
    assert len(solver.calls) == 1


async def test_login_wall_never_calls_byparr() -> None:
    http = FakeHttp(
        response=HttpResponse(url=URL, status=200, content=LOGIN.encode(), headers={})
    )
    solver = FakeSolver(result=solved_result())
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver)

    assert excinfo.value.code == "login_wall"
    assert solver.calls == []


async def test_404_is_a_miss_and_never_calls_byparr() -> None:
    http = FakeHttp(error=HttpError("not found", status=404))
    solver = FakeSolver(result=solved_result())
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver)

    assert excinfo.value.code == "not_found"
    assert solver.calls == []


async def test_solved_http_page_never_calls_byparr() -> None:
    http = FakeHttp(
        response=HttpResponse(url=URL, status=200, content=SOLVED.encode(), headers={})
    )
    solver = FakeSolver(result=solved_result())
    result = await run(http=http, solver=solver)

    assert result.title_ja
    assert solver.calls == []


async def test_byparr_failure_becomes_a_provider_error() -> None:
    http = FakeHttp(error=HttpError("forbidden", status=403))
    solver = FakeSolver(error=FlareSolverrError("byparr_circuit_open", "open"))
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver)

    assert excinfo.value.code == "byparr_circuit_open"


async def test_byparr_404_is_a_miss() -> None:
    http = FakeHttp(error=HttpError("forbidden", status=403))
    solver = FakeSolver(result=solved_result(status=404, html="<html>Not Found</html>"))
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver)

    assert excinfo.value.code == "not_found"


async def test_byparr_challenge_is_not_accepted() -> None:
    http = FakeHttp(error=HttpError("forbidden", status=403))
    solver = FakeSolver(result=solved_result(status=403, html=CHALLENGE))
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver)

    assert excinfo.value.code == "challenge_unresolved"


async def test_direct_proxy_is_refused_before_any_request() -> None:
    http = FakeHttp(
        response=HttpResponse(url=URL, status=200, content=SOLVED.encode(), headers={})
    )
    solver = FakeSolver(result=solved_result())
    with pytest.raises(ProviderError) as excinfo:
        await run(http=http, solver=solver, proxy_url=None)

    assert excinfo.value.code == "proxy_required"
    assert http.calls == 0
    assert solver.calls == []


async def test_timeout_budget_includes_byparr() -> None:
    provider = DemoProvider(
        client=FakeHttp(),  # type: ignore[arg-type]
        proxy_url="http://127.0.0.1:7890",
        timeout_seconds=15,
        flaresolverr=FakeSolver(),  # type: ignore[arg-type]
    )
    assert provider.fetch_timeout_seconds == 115
    plain = DemoProvider(
        client=FakeHttp(),  # type: ignore[arg-type]
        proxy_url="http://127.0.0.1:7890",
        timeout_seconds=15,
    )
    assert plain.fetch_timeout_seconds == 15


def test_solved_page_with_cloudflare_script_is_not_a_challenge() -> None:
    verdict = classify_page(
        status=200,
        headers={"cf-mitigated": None},
        html=SOLVED + '<script src="/cdn-cgi/challenge-platform/x.js"></script>',
        required_selectors=(".thumbnail",),
    )
    assert verdict.kind == "ok"
