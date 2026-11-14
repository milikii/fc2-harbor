"""Field-level aggregation — spec 7.2 / 7.3.

Providers are faked so the merge rules can be tested in isolation: order, no
overwrite, gap filling, preview dedupe/cap, early stop, censorship conflicts,
failure isolation and the Nyaa fallback.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from app.config import Settings
from app.db.enums import Censorship, ContentType
from app.db.models import Content
from app.network.clients import HttpClient
from app.providers.base import BaseProvider, ProviderError, ProviderPage
from app.providers.coordinator import MetadataCoordinator, build_providers
from app.providers.nyaa_fallback import NyaaFallbackProvider
from app.schemas.metadata import AssetCandidate, MetadataResult, ProviderContext

from tests.support import make_settings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def make_content(number: str = "1234567") -> Content:
    return Content(
        content_type=ContentType.FC2,
        canonical_key=f"fc2_ppv:{number}",
        display_code=f"FC2-PPV-{number}",
    )


def ctx() -> ProviderContext:
    return ProviderContext(canonical_key="fc2_ppv:1234567", display_code="FC2-PPV-1234567")


class FakeProvider(BaseProvider):
    def __init__(
        self,
        provider_id: str,
        *,
        result: MetadataResult | None = None,
        error: str | None = None,
        timeout: float = 5.0,
    ) -> None:
        super().__init__(client=HttpClient(), proxy_url=None, timeout_seconds=timeout)
        self.id = provider_id
        self._result = result
        self._error = error
        self.calls = 0

    def build_url(self, content: Content) -> str:
        return f"https://{self.id}.example/{content.canonical_key}"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        return self._result

    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        self.calls += 1
        if self._error:
            raise ProviderError(self._error, f"{self.id} failed")
        assert self._result is not None
        return self._result


def result(
    provider: str,
    *,
    title: str | None = None,
    description: str | None = None,
    cover: str | None = None,
    previews: list[str] | None = None,
    censorship: Censorship | None = None,
    official_url: str | None = None,
    release_date: datetime | None = None,
) -> MetadataResult:
    return MetadataResult(
        provider=provider,
        content_key="fc2_ppv:1234567",
        title_ja=title,
        description_ja=description,
        official_url=official_url,
        censorship=censorship,
        release_date=release_date,
        cover=(AssetCandidate(url=cover, provider=provider, role="cover") if cover else None),
        previews=[
            AssetCandidate(url=url, provider=provider, role="preview") for url in (previews or [])
        ],
    )


def coordinator(*providers: BaseProvider, settings: Settings | None = None) -> MetadataCoordinator:
    config = settings or make_settings(Path("/tmp"))
    return MetadataCoordinator(providers=list(providers), settings=config)


async def test_fields_merge_in_priority_order(tmp_path: Path) -> None:
    a = FakeProvider("A", result=result("A", title="Title A"))
    b = FakeProvider("B", result=result("B", description="Desc B", cover="https://cdn/b.jpg"))
    c = FakeProvider("C", result=result("C", previews=["https://cdn/c1.jpg"]))

    aggregated = await coordinator(a, b, c, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=1
    )

    assert aggregated.title_ja == "Title A"
    assert aggregated.description_ja == "Desc B"
    assert aggregated.cover is not None and aggregated.cover.url == "https://cdn/b.jpg"
    assert [item.url for item in aggregated.previews] == ["https://cdn/c1.jpg"]
    assert aggregated.sources["title_ja"] == "A"
    assert aggregated.sources["description_ja"] == "B"


async def test_higher_priority_value_is_never_overwritten(tmp_path: Path) -> None:
    a = FakeProvider("A", result=result("A", title="First", official_url="https://a/1"))
    b = FakeProvider("B", result=result("B", title="Second", official_url="https://b/1"))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.title_ja == "First"
    assert aggregated.official_url == "https://a/1"


async def test_early_stop_when_core_and_previews_are_complete(tmp_path: Path) -> None:
    a = FakeProvider(
        "A",
        result=result(
            "A",
            title="Title",
            description="Desc",
            previews=["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/3.jpg"],
        ),
    )
    b = FakeProvider("B", result=result("B", title="Later"))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=3
    )

    assert aggregated.stopped_early is True
    assert b.calls == 0
    assert len(aggregated.previews) == 3


async def test_previews_dedupe_and_cap(tmp_path: Path) -> None:
    a = FakeProvider(
        "A",
        result=result(
            "A",
            previews=["https://cdn/1.jpg", "https://cdn/2.jpg", "https://cdn/1.jpg"],
        ),
    )
    b = FakeProvider(
        "B",
        result=result(
            "B", previews=["https://cdn/2.jpg", "https://cdn/3.jpg", "https://cdn/4.jpg"]
        ),
    )

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), max_previews=3, min_previews=9
    )

    assert [item.url for item in aggregated.previews] == [
        "https://cdn/1.jpg",
        "https://cdn/2.jpg",
        "https://cdn/3.jpg",
    ]


async def test_censorship_conflict_becomes_unknown(tmp_path: Path) -> None:
    a = FakeProvider("A", result=result("A", censorship=Censorship.CENSORED))
    b = FakeProvider("B", result=result("B", censorship=Censorship.UNCENSORED))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.censorship is Censorship.UNKNOWN
    assert aggregated.sources["censorship"].startswith("conflict:")


async def test_single_censorship_tag_is_kept(tmp_path: Path) -> None:
    a = FakeProvider("A", result=result("A", censorship=Censorship.THIN))
    b = FakeProvider("B", result=result("B", censorship=Censorship.THIN))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.censorship is Censorship.THIN
    assert aggregated.sources["censorship"] == "A"


async def test_not_found_is_a_miss_not_a_failure(tmp_path: Path) -> None:
    a = FakeProvider("A", error="not_found")
    b = FakeProvider("B", result=result("B", title="Title B"))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.runs[0].success is True
    assert aggregated.runs[0].error_code == "not_found"
    assert aggregated.title_ja == "Title B"
    assert coordinator(a).health() == {} or coordinator(a).health().get("A", 0) == 0


async def test_provider_failure_does_not_stop_the_chain(tmp_path: Path) -> None:
    a = FakeProvider("A", error="http_error")
    b = FakeProvider("B", result=result("B", title="Title B"))

    aggregated = await coordinator(a, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.title_ja == "Title B"
    assert [run.success for run in aggregated.runs] == [False, True]
    assert aggregated.runs[0].error_code == "http_error"


async def test_provider_timeout_is_isolated(tmp_path: Path) -> None:
    class SlowProvider(FakeProvider):
        async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
            import asyncio

            await asyncio.sleep(1)
            raise AssertionError("should have timed out")

    slow = SlowProvider("Slow", timeout=0.01)
    b = FakeProvider("B", result=result("B", title="Title B"))

    aggregated = await coordinator(slow, b, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.runs[0].error_code == "timeout"
    assert aggregated.title_ja == "Title B"


async def test_release_date_and_official_url_are_merged(tmp_path: Path) -> None:
    released = datetime(2026, 11, 1, tzinfo=UTC)
    a = FakeProvider("A", result=result("A", release_date=released, official_url="https://a/1"))

    aggregated = await coordinator(a, settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )

    assert aggregated.release_date == released
    assert aggregated.official_url == "https://a/1"


async def test_nyaa_fallback_only_fills_missing_fields(tmp_path: Path) -> None:
    body = (FIXTURES / "providers" / "nyaa_view.html").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    fallback = NyaaFallbackProvider(
        client=HttpClient(transport=httpx.MockTransport(handler)),
        proxy_url=None,
        timeout_seconds=5,
    )
    a = FakeProvider("A", result=result("A", title="FC2-PPV-1234567 from A"))

    engine = coordinator(a, settings=make_settings(tmp_path))
    engine.nyaa_fallback = fallback
    aggregated_result = await engine.aggregate(
        make_content(),
        ProviderContext(
            canonical_key="fc2_ppv:1234567",
            release_url="https://sukebei.nyaa.si/view/4761220",
        ),
        min_previews=1,
    )

    assert aggregated_result.title_ja == "FC2-PPV-1234567 from A"  # not overwritten
    assert aggregated_result.description_ja and "作品" in aggregated_result.description_ja
    assert len(aggregated_result.previews) == 2
    assert aggregated_result.runs[-1].provider == "Nyaa"


async def test_fallback_skipped_when_everything_is_present(tmp_path: Path) -> None:
    body = (FIXTURES / "providers" / "nyaa_view.html").read_bytes()
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(200, content=body, headers={"content-type": "text/html"})

    fallback = NyaaFallbackProvider(
        client=HttpClient(transport=httpx.MockTransport(handler)),
        proxy_url=None,
        timeout_seconds=5,
    )
    a = FakeProvider(
        "A",
        result=result("A", title="Title", description="Desc", previews=["https://cdn/1.jpg"]),
    )
    engine = coordinator(a, settings=make_settings(tmp_path))
    engine.nyaa_fallback = fallback

    aggregated = await engine.aggregate(make_content(), ctx(), min_previews=1)

    assert calls["n"] == 0
    assert all(run.provider != "Nyaa" for run in aggregated.runs)


def test_build_providers_keeps_configured_order(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    providers, fallback = build_providers(settings, client=HttpClient(), router=_router(settings))

    assert [provider.id for provider in providers] == list(settings.provider.priorities["jav_fc2"])
    assert fallback.id == "Nyaa"


def test_build_providers_skips_disabled_when_relaxed(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        provider={"strict_enabled": False, "sources": {"Madou": {"enabled": False}}},
    )
    providers, _fallback = build_providers(settings, client=HttpClient(), router=_router(settings))

    assert "Madou" not in [provider.id for provider in providers]
    assert len(providers) == 8


def _router(settings: Settings):
    from app.network.proxy import ProxyRouter

    return ProxyRouter(settings.proxy_profiles)


@pytest.mark.parametrize("provider_count", [0])
async def test_no_providers_yields_empty_aggregate(tmp_path: Path, provider_count: int) -> None:
    aggregated = await coordinator(settings=make_settings(tmp_path)).aggregate(
        make_content(), ctx(), min_previews=0
    )
    assert aggregated.title_ja is None
    assert aggregated.runs == []
