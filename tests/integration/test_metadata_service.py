"""Metadata enrichment end to end — spec 7.2 / 8.4 / 12.3 / 12.4.

Real database, real image validation, fake HTTP: one content goes through the
coordinator, provider_results, the content-addressed image cache, the assets table
and translation.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import httpx
from app.config import CANONICAL_FC2_PROVIDER_ORDER, Settings
from app.db.enums import AssetRole, Censorship, ContentType, MetadataStatus
from app.db.models import Asset, Content, ProviderResult, Release
from app.db.session import Database
from app.network.image_fetcher import ImageFetcher, ImageFetchError
from app.network.proxy import ProxyRouter
from app.providers.base import BaseProvider, ProviderError, ProviderPage
from app.providers.coordinator import MetadataCoordinator
from app.schemas.metadata import AssetCandidate, MetadataResult, ProviderContext
from app.services.metadata import (
    MetadataService,
    derive_image_allowlist,
    normalize_image_url,
    registered_domain,
)
from app.services.translate import OpenAITranslator
from PIL import Image
from sqlalchemy import func, select

from tests.support import make_settings

#: Long enough to pass the "is this a real description?" filter (>= 20 chars).
SAMPLE_JA = "素人初撮り。19歳の大学生が人生で初めての撮影に挑戦しました。"
FALLBACK_JA = "フォールバックが提供した説明文です。十分に長い文章になっています。"

HASH = "0123456789abcdef0123456789abcdef01234567"


def png_bytes(*, seed: int = 0) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (16 + seed % 5, 9), (seed % 255, 120, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


class FakeProvider(BaseProvider):
    def __init__(self, provider_id: str, result: MetadataResult) -> None:
        super().__init__(
            client=httpx.AsyncClient(trust_env=False), proxy_url=None, timeout_seconds=5
        )
        self.id = provider_id
        self._result = result

    def build_url(self, content: Content) -> str:
        return "https://example.invalid/"

    def parse(self, page: ProviderPage, content: Content) -> MetadataResult | None:
        return self._result

    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        return self._result


class FailingProvider(FakeProvider):
    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        raise ProviderError("structure_changed", "broken page")


def result(*, previews: list[str]) -> MetadataResult:
    return MetadataResult(
        provider="Fc2",
        content_key="fc2_ppv:1234567",
        title_ja="FC2-PPV-1234567 タイトル",
        description_ja=SAMPLE_JA,
        official_url="https://adult.contents.fc2.com/article/1234567/",
        censorship=Censorship.UNCENSORED,
        release_date=datetime(2020, 11, 1, tzinfo=UTC),
        cover=AssetCandidate(url="https://img.example.com/cover.jpg", provider="Fc2", role="cover"),
        previews=[AssetCandidate(url=url, provider="Fc2", role="preview") for url in previews],
    )


def direct_provider_settings(tmp_path: Path, **overrides: object) -> Settings:
    sources = {name: {"proxy": "direct", "enabled": True} for name in CANONICAL_FC2_PROVIDER_ORDER}
    base: dict[str, object] = {
        "provider": {"sources": sources},
        "nyaa": {"proxy": "direct"},
        "image_fetch": {
            "cache_dir": str(tmp_path / "cache"),
            "allowed_hosts": ["img.example.com"],
            "max_previews": 2,
            "min_preview_images": 1,
        },
        "translation": {
            "enabled": True,
            "base_url": "https://translate.example/v1",
            "api_key": "test-key",
            "model": "test-model",
        },
    }
    base.update(overrides)
    return make_settings(tmp_path, **base)  # type: ignore[arg-type]


async def seed_content(db: Database) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
        )
        session.add(content)
        await session.flush()
        session.add(
            Release(
                content_id=content.id,
                source="nyaa",
                source_item_id="4761220",
                source_url="https://sukebei.nyaa.si/view/4761220",
                magnet=f"magnet:?xt=urn:btih:{HASH}",
                info_hash=HASH,
                published_at=datetime.now(UTC),
            )
        )
        return content.id


def build_service(
    db: Database,
    settings: Settings,
    tmp_path: Path,
    *,
    provider_result: MetadataResult | None = None,
    failing: bool = False,
    image_status: int = 200,
) -> MetadataService:
    def image_handler(request: httpx.Request) -> httpx.Response:
        if image_status != 200:
            return httpx.Response(image_status)
        # Distinct bytes per path: identical images are deduped by SHA-256.
        seed = int(hashlib.sha256(request.url.path.encode()).hexdigest()[:4], 16)
        body = png_bytes(seed=seed)
        return httpx.Response(200, content=body, headers={"content-type": "image/png"})

    def translate_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": [{"message": {"content": "中文译文"}}]})

    provider = (
        FailingProvider("Fc2", result(previews=[]))
        if failing
        else FakeProvider(
            "Fc2",
            provider_result
            or result(
                previews=[
                    "https://img.example.com/p1.jpg",
                    "https://img.example.com/p2.jpg",
                    "https://img.example.com/p3.jpg",
                ]
            ),
        )
    )
    coordinator = MetadataCoordinator(providers=[provider], settings=settings)
    fetcher = ImageFetcher(
        cache_dir=Path(settings.image_fetch.cache_dir),
        allowed_hosts=settings.image_fetch.allowed_hosts,
        transport=httpx.MockTransport(image_handler),
        resolver=lambda host: ["93.184.216.34"],
    )
    translator = OpenAITranslator(
        base_url="https://translate.example/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(translate_handler),
    )
    return MetadataService(
        db=db,
        coordinator=coordinator,
        fetcher=fetcher,
        settings=settings,
        router=ProxyRouter(settings.proxy_profiles),
        translator=translator,
    )


async def test_enrich_persists_metadata_assets_and_translation(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path)

    result_obj = await service.enrich(content_id)

    assert result_obj.metadata_complete is True
    assert result_obj.description_zh == "中文译文"
    assert result_obj.translated is True
    assert len(result_obj.preview_asset_ids) == 2  # capped by max_previews
    assert result_obj.cover_asset_id is not None
    assert result_obj.image_errors == []

    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        assert content.title_ja == "FC2-PPV-1234567 タイトル"
        assert content.description_ja == SAMPLE_JA
        assert content.description_zh == "中文译文"
        assert content.official_url == "https://adult.contents.fc2.com/article/1234567/"
        assert content.censorship is Censorship.UNCENSORED
        assert content.metadata_status is MetadataStatus.COMPLETE

        assets = list(
            (await session.scalars(select(Asset).where(Asset.content_id == content_id))).all()
        )
        provider_results = await session.scalar(select(func.count()).select_from(ProviderResult))

    assert len(assets) == 3
    assert {asset.role for asset in assets} == {AssetRole.COVER, AssetRole.PREVIEW}
    for asset in assets:
        assert asset.sha256 and len(asset.sha256) == 64
        assert asset.local_path and Path(asset.local_path).is_file()  # noqa: ASYNC240
        assert asset.width >= 16 and asset.height == 9
        assert asset.proxy_profile == "direct"
    assert provider_results == 1


async def test_second_enrich_does_not_duplicate_assets(db: Database, tmp_path: Path) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path)

    await service.enrich(content_id)
    await service.enrich(content_id)

    async with db.session() as session:
        assets = await session.scalar(
            select(func.count()).select_from(Asset).where(Asset.content_id == content_id)
        )
        results = await session.scalar(select(func.count()).select_from(ProviderResult))
    assert assets == 3
    assert results == 2  # one row per attempt, per spec 12.3


async def test_image_failure_keeps_metadata(db: Database, tmp_path: Path) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path, image_status=404)

    result_obj = await service.enrich(content_id)

    assert result_obj.image_errors
    assert result_obj.cover_asset_id is None
    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None and content.title_ja
        assets = await session.scalar(select(func.count()).select_from(Asset))
    assert assets == 0


async def test_provider_failure_is_recorded(db: Database, tmp_path: Path) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path, failing=True)

    result_obj = await service.enrich(content_id)

    assert result_obj.metadata_complete is False
    assert result_obj.provider_runs[0].success is False
    async with db.session() as session:
        row = await session.scalar(select(ProviderResult))
        content = await session.get(Content, content_id)
    assert row is not None and row.error_code == "structure_changed"
    assert content is not None and content.metadata_status is MetadataStatus.PARTIAL


def test_normalize_image_url_strips_fragment_and_normalises_host() -> None:
    assert (
        normalize_image_url("HTTPS://Img.Example.COM/a.jpg?x=1#frag")
        == "https://img.example.com/a.jpg?x=1"
    )


def test_registered_domain_handles_second_level_suffixes() -> None:
    assert registered_domain("adult.contents.fc2.com") == "fc2.com"
    assert registered_domain("sukebei.nyaa.si") == "nyaa.si"
    assert registered_domain("www.example.co.jp") == "example.co.jp"
    assert registered_domain("localhost") == "localhost"


def test_image_allowlist_derives_from_providers(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    allowed = derive_image_allowlist(settings)
    assert "fc2.com" in allowed
    assert "javdb.com" in allowed
    assert "nyaa.si" in allowed


def test_explicit_allowlist_wins(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, image_fetch={"allowed_hosts": ["cdn.example.com"]})
    assert derive_image_allowlist(settings) == ["cdn.example.com"]


class _FailingTranslator:
    """Stands in for a blocked Google endpoint."""

    def __init__(self) -> None:
        self.calls = 0

    async def translate(self, text: str | None) -> str | None:
        from app.services.translate import TranslationError

        self.calls += 1
        raise TranslationError("non_json", "blocked: HTML interstitial")


async def test_translation_failure_leaves_description_empty(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path)
    translator = _FailingTranslator()
    service.translator = translator

    result_obj = await service.enrich(content_id)

    assert translator.calls == 1
    assert result_obj.description_zh is None
    assert result_obj.metadata_complete is True
    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        assert content.description_ja == SAMPLE_JA
        assert content.description_zh is None


def test_build_translator_wires_the_google_endpoint_and_proxy(
    tmp_path: Path,
) -> None:
    from app.services.metadata import build_translator
    from app.services.translate import FallbackTranslator, GoogleFreeTranslator

    settings = direct_provider_settings(tmp_path)
    settings = settings.model_copy(
        update={
            "translation": settings.translation.model_copy(
                update={"enabled": True, "proxy": "direct"}
            )
        }
    )
    translator = build_translator(settings)

    # Google is the primary; MyMemory is the fallback (deviation §53).
    assert isinstance(translator, FallbackTranslator)
    assert isinstance(translator._primary, GoogleFreeTranslator)


def test_build_translator_can_disable_the_fallback(tmp_path: Path) -> None:
    from app.services.metadata import build_translator
    from app.services.translate import FallbackTranslator

    settings = direct_provider_settings(tmp_path)
    settings = settings.model_copy(
        update={
            "translation": settings.translation.model_copy(
                update={"enabled": True, "proxy": "direct", "fallback_provider": "none"}
            )
        }
    )
    translator = build_translator(settings)

    assert isinstance(translator, FallbackTranslator)
    assert translator._fallback is None


def test_build_translator_returns_none_when_disabled(tmp_path: Path) -> None:
    from app.services.metadata import build_translator

    settings = direct_provider_settings(tmp_path)
    settings = settings.model_copy(
        update={"translation": settings.translation.model_copy(update={"enabled": False})}
    )
    assert build_translator(settings) is None


# -- translation wiring -------------------------------------------------------


class _CountingTranslator:
    def __init__(self, value: str = "中文译文") -> None:
        self.calls: list[str] = []
        self.value = value

    async def translate(self, text: str | None) -> str | None:
        self.calls.append(text or "")
        return self.value

    async def aclose(self) -> None:
        return None


def service_with(
    db: Database, settings: Settings, tmp_path: Path, translator: object
) -> MetadataService:
    service = build_service(db, settings, tmp_path)
    service.translator = translator  # type: ignore[assignment]
    return service


async def test_translation_runs_for_a_present_description(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    translator = _CountingTranslator()
    service = service_with(db, settings, tmp_path, translator)

    translated = await service._translate(content_id, SAMPLE_JA)

    assert translator.calls == [SAMPLE_JA]
    assert translated == "中文译文"
    async with db.session() as session:
        content = await session.get(Content, content_id)
    assert content is not None
    assert content.description_zh == "中文译文"


async def test_translation_is_skipped_when_there_is_no_description(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    translator = _CountingTranslator()
    service = service_with(db, settings, tmp_path, translator)

    assert await service._translate(content_id, None) is None
    assert await service._translate(content_id, "   ") is None
    assert translator.calls == []


async def test_translation_is_skipped_when_disabled(db: Database, tmp_path: Path) -> None:
    from app.services.metadata import build_translator

    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    settings = settings.model_copy(
        update={"translation": settings.translation.model_copy(update={"enabled": False})}
    )
    assert build_translator(settings) is None

    service = service_with(db, settings, tmp_path, None)
    assert await service._translate(content_id, SAMPLE_JA) is None


async def test_cached_translation_is_reused(db: Database, tmp_path: Path) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.description_zh = "已有译文"

    translator = _CountingTranslator("新しい訳")
    service = service_with(db, settings, tmp_path, translator)

    assert await service._translate(content_id, SAMPLE_JA) == "已有译文"
    assert translator.calls == []


async def test_empty_string_residue_is_retranslated(db: Database, tmp_path: Path) -> None:
    """A previous failure may have left ``''``; that is not a cache hit."""

    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.description_zh = ""

    translator = _CountingTranslator("重新翻译")
    service = service_with(db, settings, tmp_path, translator)

    assert await service._translate(content_id, SAMPLE_JA) == "重新翻译"
    assert translator.calls == [SAMPLE_JA]
    async with db.session() as session:
        content = await session.get(Content, content_id)
    assert content is not None
    assert content.description_zh == "重新翻译"


async def test_fallback_description_reaches_the_translator(
    db: Database, tmp_path: Path
) -> None:
    """A description that only the Nyaa fallback supplied must still be translated."""

    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    translator = _CountingTranslator("回退译文")
    service = service_with(db, settings, tmp_path, translator)

    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.description_zh = None

    translated = await service._translate(
        content_id, FALLBACK_JA
    )
    assert translator.calls == [FALLBACK_JA]
    assert translated == "回退译文"


# -- viewer-page resolution is enabled only for the Nyaa fallback --------------


class _RecordingFetcher:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def fetch(self, url: str, **kwargs: object) -> object:
        self.calls.append({"url": url, **kwargs})
        raise ImageFetchError("bad_content_type", "stop here")


async def test_viewer_resolution_is_only_enabled_for_nyaa_images(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    service = build_service(db, settings, tmp_path)
    recorder = _RecordingFetcher()
    service.fetcher = recorder  # type: ignore[assignment]

    for provider in ("Nyaa", "Fc2"):
        await service._store_one(
            content_id,
            f"https://cdn.example.com/{provider}.jpg",
            provider,
            AssetRole.PREVIEW,
            0,
            None,
            set(),
        )

    assert recorder.calls[0]["resolve_viewer"] is True
    assert recorder.calls[1]["resolve_viewer"] is False


# -- only the official description is translated ------------------------------


async def test_a_url_only_description_is_not_translated(
    db: Database, tmp_path: Path
) -> None:
    """No point spending a request on a bare image URL."""

    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    translator = _CountingTranslator()
    service = service_with(db, settings, tmp_path, translator)

    translated = await service._translate(
        content_id, "https://imagetwist.com/367wmg0gouoq/grewag.png"
    )

    assert translated is None
    assert translator.calls == []


async def test_a_promotional_description_is_not_translated(
    db: Database, tmp_path: Path
) -> None:
    content_id = await seed_content(db)
    settings = direct_provider_settings(tmp_path)
    translator = _CountingTranslator()
    service = service_with(db, settings, tmp_path, translator)

    translated = await service._translate(
        content_id, "★ 特価★★ 3960pt→1980pt 期間限定キャンペーン実施中です。"
    )

    assert translated is None
    assert translator.calls == []
