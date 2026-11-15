"""Config-driven provider: add a site in YAML, no code change.

Fixtures are constructed here; nothing talks to a real site.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from app.config import ConfigError, Settings
from app.db.enums import ContentType
from app.db.models import Content
from app.network.clients import HttpClient
from app.providers.base import ProviderError, ProviderPage
from app.providers.custom import CustomProvider, build_custom_providers
from app.schemas.metadata import ProviderContext

from tests.support import make_config, make_settings


def make_content(number: str = "1234567") -> Content:
    return Content(
        content_type=ContentType.FC2,
        canonical_key=f"fc2_ppv:{number}",
        display_code=f"FC2-PPV-{number}",
    )


SEARCH_HTML = """
<html><body>
  <div class="item">
    <a class="title" href="/v/abc123">FC2-PPV-1234567 サンプル作品</a>
    <img data-src="/img/cover.jpg">
    <p class="desc">説明文です。</p>
    <div class="gallery"><img data-src="/img/1.jpg"><img data-src="/img/2.jpg"></div>
  </div>
  <div class="item">
    <a class="title" href="/v/zzz999">FC2-PPV-9999999 別の作品</a>
  </div>
</body></html>
"""

DETAIL_HTML = """
<html><body>
  <h1 class="title">FC2-PPV-1234567 詳細タイトル</h1>
  <p class="desc">詳細ページの長い説明。</p>
  <img data-src="/img/detail-cover.jpg">
  <div class="gallery"><img data-src="/img/d1.jpg"></div>
</body></html>
"""


def settings_with_custom(tmp_path: Path, **overrides: object) -> Settings:
    entry: dict[str, object] = {
        "name": "some_site",
        "search_url": "https://some.example/search?q={code}",
        "proxy": "direct",
        "selectors": {
            "card": ".item",
            "title": ".title",
            "link": "a[href]",
            "cover": "img[data-src]",
            "description": ".desc",
            "previews": ".gallery img",
        },
    }
    entry.update(overrides)
    return make_settings(tmp_path, provider={"custom": [entry]})


def test_config_parses_a_custom_provider(tmp_path: Path) -> None:
    settings = settings_with_custom(tmp_path)
    entry = settings.provider.custom[0]
    assert entry.name == "some_site"
    assert entry.enabled is True
    assert entry.selectors.card == ".item"


def test_search_url_must_contain_the_code_placeholder(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["provider"]["custom"] = [
        {"name": "x", "search_url": "https://x.example/search", "proxy": "direct"}
    ]
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "{code}" in str(excinfo.value)


def test_custom_name_must_not_collide_with_a_builtin(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["provider"]["custom"] = [
        {"name": "Fc2", "search_url": "https://x.example/{code}", "proxy": "direct"}
    ]
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_duplicate_custom_names_are_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    entry = {"name": "x", "search_url": "https://x.example/{code}", "proxy": "direct"}
    config["provider"]["custom"] = [entry, dict(entry)]
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "duplicate" in str(excinfo.value)


def test_unknown_proxy_is_rejected(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["provider"]["custom"] = [
        {"name": "x", "search_url": "https://x.example/{code}", "proxy": "nope"}
    ]
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "provider.custom.x.proxy" in str(excinfo.value)


def provider_for(settings: Settings, handler: object) -> CustomProvider:
    client = HttpClient(transport=httpx.MockTransport(handler))  # type: ignore[arg-type]
    return CustomProvider(
        config=settings.provider.custom[0],
        client=client,
        proxy_url=None,
        timeout_seconds=5,
    )


def page(html: str = SEARCH_HTML) -> ProviderPage:
    return ProviderPage(url="https://some.example/search?q=FC2-PPV-1234567", html=html, status=200)


def test_selectors_extract_the_matching_card(tmp_path: Path) -> None:
    settings = settings_with_custom(tmp_path)
    result = provider_for(settings, lambda r: httpx.Response(200, text="")).parse(
        page(), make_content()
    )

    assert result is not None
    assert result.title_ja == "FC2-PPV-1234567 サンプル作品"
    assert result.description_ja == "説明文です。"
    assert result.official_url == "https://some.example/v/abc123"
    assert result.cover is not None
    assert result.cover.url == "https://some.example/img/cover.jpg"
    assert [item.url for item in result.previews] == [
        "https://some.example/img/1.jpg",
        "https://some.example/img/2.jpg",
    ]


def test_a_card_without_the_code_is_a_miss(tmp_path: Path) -> None:
    settings = settings_with_custom(tmp_path)
    result = provider_for(settings, lambda r: httpx.Response(200, text="")).parse(
        page(), make_content("7654321")
    )
    assert result is None


def test_missing_optional_selectors_degrade(tmp_path: Path) -> None:
    settings = settings_with_custom(
        tmp_path, selectors={"card": ".item", "title": ".title", "link": "a[href]"}
    )
    result = provider_for(settings, lambda r: httpx.Response(200, text="")).parse(
        page(), make_content()
    )

    assert result is not None
    assert result.title_ja
    assert result.cover is None
    assert result.description_ja is None
    assert result.previews == []


def test_structure_change_is_reported_not_silently_empty(tmp_path: Path) -> None:
    settings = settings_with_custom(tmp_path)
    adapter = provider_for(settings, lambda r: httpx.Response(200, text=""))
    with pytest.raises(ProviderError) as excinfo:
        adapter.parse(page("<html><body>no cards here</body></html>"), make_content())
    assert excinfo.value.code == "structure_changed"


async def test_detail_page_supplies_missing_fields(tmp_path: Path) -> None:
    settings = settings_with_custom(
        tmp_path,
        detail_url="https://some.example/v/{id}",
        selectors={
            "card": ".item",
            "title": ".title",
            "link": "a[href]",
            "cover": "img[data-src]",
            "description": ".desc",
            "previews": ".gallery img",
        },
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v/"):
            return httpx.Response(200, text=DETAIL_HTML)
        return httpx.Response(200, text=SEARCH_HTML)

    adapter = provider_for(settings, handler)
    content = make_content()
    search = page()
    extra = await adapter.fetch_extra(
        search, content, ProviderContext(canonical_key="fc2_ppv:1234567")
    )
    assert "detail_html" in extra
    assert extra["detail_url"] == "https://some.example/v/abc123"

    result = adapter.parse(
        ProviderPage(url=search.url, html=search.html, status=200, extra=extra), content
    )
    assert result is not None
    assert result.description_ja == "詳細ページの長い説明。"
    assert result.cover is not None and result.cover.url == "https://some.example/img/detail-cover.jpg"
    assert result.raw["detail_fetched"] is True


def test_build_custom_providers_runs_after_the_builtins(tmp_path: Path) -> None:
    from app.network.proxy import ProxyRouter
    from app.providers.coordinator import build_providers

    settings = settings_with_custom(tmp_path)
    providers, _fallback = build_providers(
        settings,
        client=HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=""))),
        router=ProxyRouter(settings.proxy_profiles),
    )

    assert providers[-1].id == "some_site"
    assert len(providers) == len(settings.provider.priorities["jav_fc2"]) + 1


def test_disabled_custom_provider_is_skipped(tmp_path: Path) -> None:
    from app.network.proxy import ProxyRouter

    settings = settings_with_custom(tmp_path, enabled=False)
    built = build_custom_providers(
        settings,
        client=HttpClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=""))),
        router=ProxyRouter(settings.proxy_profiles),
    )
    assert built == []
