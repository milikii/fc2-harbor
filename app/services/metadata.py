"""Metadata enrichment service — spec 7.2 / 8.4 / 12.3 / 12.4.

Ties the pieces together for one content:

1. run the nine-provider aggregation (plus the Nyaa fallback);
2. persist merged fields on ``contents`` without overwriting what is already
   there, and record one ``provider_results`` row per attempt (success or not);
3. download cover + previews through the routed proxy, validate them, store them
   content-addressed and write ``assets`` rows (dedupe by URL and SHA-256);
4. translate the Japanese description when translation is enabled.

Network work happens outside database transactions; every write is short and
conditional. An image or translation failure never loses metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import Settings
from app.db.enums import AssetRole, Censorship, MetadataStatus
from app.db.models import Asset, Content, ProviderResult, Release, utcnow
from app.db.session import Database
from app.logging import get_logger
from app.network.image_fetcher import FetchedImage, ImageFetcher, ImageFetchError
from app.network.proxy import ProxyRouter
from app.providers.coordinator import AggregatedMetadata, MetadataCoordinator, ProviderRun
from app.schemas.metadata import ProviderContext
from app.services.translate import OpenAITranslator, TranslationError

_log = get_logger("metadata.service")


@dataclass(slots=True)
class EnrichmentResult:
    content_id: str
    title_ja: str | None = None
    description_ja: str | None = None
    description_zh: str | None = None
    official_url: str | None = None
    censorship: Censorship = Censorship.UNKNOWN
    cover_asset_id: str | None = None
    preview_asset_ids: list[str] = field(default_factory=list)
    provider_runs: list[ProviderRun] = field(default_factory=list)
    image_errors: list[str] = field(default_factory=list)
    translated: bool = False

    @property
    def metadata_complete(self) -> bool:
        return bool(self.title_ja and self.description_ja)


def normalize_image_url(url: str) -> str:
    """Canonical form used for asset dedupe (spec 7.2 rule 6)."""

    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = f":{parts.port}" if parts.port else ""
    path = parts.path or "/"
    return urlunsplit((scheme, f"{host}{port}", path, parts.query, ""))


def registered_domain(host: str) -> str:
    """Best-effort eTLD+1 without shipping a public suffix list."""

    labels = [label for label in host.lower().split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    second_level = {"co", "com", "ne", "or", "ac", "go", "gr", "ed"}
    if labels[-2] in second_level and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def derive_image_allowlist(settings: Settings) -> list[str]:
    """Hosts allowed to serve images.

    Explicit ``image_fetch.allowed_hosts`` wins. Otherwise the registered domains
    of the configured providers, the CDN routes and Nyaa are allowed, so a provider
    CDN on the same domain works while an unrelated host does not.
    """

    if settings.image_fetch.allowed_hosts:
        return sorted({host.lower() for host in settings.image_fetch.allowed_hosts})
    hosts: set[str] = set()
    for source in settings.provider.sources.values():
        hosts.add(registered_domain(source.host))
    hosts.add(registered_domain(urlsplit(settings.nyaa.base_url).hostname or ""))
    for domain in settings.image_fetch.cdn_routes:
        hosts.add(registered_domain(domain))
    return sorted(host for host in hosts if host)


class MetadataService:
    def __init__(
        self,
        *,
        db: Database,
        coordinator: MetadataCoordinator,
        fetcher: ImageFetcher,
        settings: Settings,
        router: ProxyRouter,
        translator: OpenAITranslator | None = None,
    ) -> None:
        self.db = db
        self.coordinator = coordinator
        self.fetcher = fetcher
        self.settings = settings
        self.router = router
        self.translator = translator

    async def enrich(self, content_id: str) -> EnrichmentResult:
        content, release = await self._load(content_id)
        result = EnrichmentResult(content_id=content_id)

        ctx = self._context(content, release)
        aggregated = await self.coordinator.aggregate(content, ctx)
        result.provider_runs = aggregated.runs

        await self._persist_metadata(content_id, aggregated)
        result.title_ja = aggregated.title_ja
        result.description_ja = aggregated.description_ja
        result.official_url = aggregated.official_url
        result.censorship = aggregated.censorship

        cover_id, preview_ids, errors = await self._store_assets(content_id, aggregated)
        result.cover_asset_id = cover_id
        result.preview_asset_ids = preview_ids
        result.image_errors = errors

        translated = await self._translate(content_id, aggregated.description_ja)
        result.description_zh = translated
        result.translated = bool(translated)
        return result

    # -- loading ------------------------------------------------------------

    async def _load(self, content_id: str) -> tuple[Content, Release | None]:
        async with self.db.session() as session:
            content = await session.get(Content, content_id)
            if content is None:
                raise LookupError(f"content {content_id} not found")
            release = await session.scalar(
                select(Release)
                .where(Release.content_id == content_id)
                .order_by(Release.published_at.asc().nullslast(), Release.created_at)
                .limit(1)
            )
            return content, release

    def _context(self, content: Content, release: Release | None) -> ProviderContext:
        first_provider = self.settings.provider.priorities["jav_fc2"][0]
        proxy_name = self.settings.provider.sources[first_provider].proxy
        return ProviderContext(
            canonical_key=content.canonical_key,
            display_code=content.display_code,
            proxy_profile=proxy_name,
            timeout_seconds=self.settings.provider.sources[first_provider].timeout_seconds,
            max_previews=self.settings.image_fetch.max_previews,
            release_url=release.source_url if release else None,
            release_item_id=release.source_item_id if release else None,
        )

    # -- persistence --------------------------------------------------------

    async def _persist_metadata(self, content_id: str, aggregated: AggregatedMetadata) -> None:
        async with self.db.transaction() as session:
            content = await session.get(Content, content_id)
            if content is None:
                return
            if aggregated.title_ja and not content.title_ja:
                content.title_ja = aggregated.title_ja
            if aggregated.description_ja and not content.description_ja:
                content.description_ja = aggregated.description_ja
            if aggregated.official_url and not content.official_url:
                content.official_url = aggregated.official_url
            if (
                aggregated.censorship is not Censorship.UNKNOWN
                and content.censorship is Censorship.UNKNOWN
            ):
                content.censorship = aggregated.censorship
            content.metadata_status = (
                MetadataStatus.COMPLETE
                if (content.title_ja and content.description_ja)
                else MetadataStatus.PARTIAL
            )
            content.updated_at = utcnow()

            for run in aggregated.runs:
                session.add(
                    ProviderResult(
                        content_id=content_id,
                        provider=run.provider,
                        success=run.success,
                        http_status=run.http_status,
                        error_code=run.error_code,
                        latency_ms=run.latency_ms,
                        result_json=run.result_json,
                        asset_count=run.asset_count,
                    )
                )

    # -- assets -------------------------------------------------------------

    async def _store_assets(
        self, content_id: str, aggregated: AggregatedMetadata
    ) -> tuple[str | None, list[str], list[str]]:
        errors: list[str] = []
        existing = await self._existing_urls(content_id)

        cover_id = None
        if aggregated.cover is not None:
            cover_id, error = await self._store_one(
                content_id,
                aggregated.cover.url,
                aggregated.cover.provider,
                AssetRole.COVER,
                0,
                aggregated.cover.referer,
                existing,
            )
            if error:
                errors.append(f"cover:{error}")

        preview_ids: list[str] = []
        for position, candidate in enumerate(aggregated.previews):
            if len(preview_ids) >= self.settings.image_fetch.max_previews:
                break
            asset_id, error = await self._store_one(
                content_id,
                candidate.url,
                candidate.provider,
                AssetRole.PREVIEW,
                position,
                candidate.referer,
                existing,
            )
            if error:
                errors.append(f"preview{position}:{error}")
                continue
            if asset_id:
                preview_ids.append(asset_id)
        return cover_id, preview_ids, errors

    async def _existing_urls(self, content_id: str) -> set[str]:
        async with self.db.session() as session:
            rows = await session.scalars(
                select(Asset.normalized_url).where(
                    Asset.content_id == content_id, Asset.normalized_url.is_not(None)
                )
            )
            return {url for url in rows if url}

    async def _store_one(
        self,
        content_id: str,
        url: str,
        provider: str,
        role: AssetRole,
        position: int,
        referer: str | None,
        existing: set[str],
    ) -> tuple[str | None, str | None]:
        normalized = normalize_image_url(url)
        if normalized in existing:
            return None, None

        proxy = self.router.for_image(
            self.settings.provider.sources[provider].proxy
            if provider in self.settings.provider.sources
            else self.settings.nyaa.proxy,
            urlsplit(url).hostname,
            self.settings.image_fetch.cdn_routes,
        )
        try:
            image: FetchedImage = await self.fetcher.fetch(
                url, proxy_url=proxy.url, referer=referer
            )
        except ImageFetchError as exc:
            _log.warning(
                "metadata.image_rejected",
                content_id=content_id,
                provider=provider,
                error_code=exc.code,
            )
            return None, exc.code

        try:
            async with self.db.transaction() as session:
                asset = Asset(
                    content_id=content_id,
                    provider=provider,
                    role=role,
                    original_url=url,
                    final_url=image.final_url,
                    normalized_url=normalized,
                    cdn_host=image.cdn_host,
                    proxy_profile=proxy.profile,
                    sha256=image.sha256,
                    local_path=str(image.path),
                    width=image.width,
                    height=image.height,
                    size_bytes=image.size_bytes,
                    position=position,
                )
                session.add(asset)
                await session.flush()
                existing.add(normalized)
                return asset.id, None
        except IntegrityError:
            # Same URL or same bytes already stored for this content.
            return None, None

    # -- translation --------------------------------------------------------

    async def _translate(self, content_id: str, description: str | None) -> str | None:
        if self.translator is None or not description:
            return None
        async with self.db.session() as session:
            content = await session.get(Content, content_id)
            if content is None or content.description_zh:
                return content.description_zh if content else None
        try:
            translated = await self.translator.translate(description)
        except TranslationError as exc:
            _log.warning("metadata.translate_failed", content_id=content_id, error_code=exc.code)
            return None
        if not translated:
            return None
        async with self.db.transaction() as session:
            content = await session.get(Content, content_id)
            if content is not None and not content.description_zh:
                content.description_zh = translated
                content.updated_at = utcnow()
        return translated


def build_translator(settings: Settings) -> OpenAITranslator | None:
    config = settings.translation
    if not config.enabled:
        return None
    if not (config.base_url and config.api_key and config.model):
        return None
    return OpenAITranslator(
        base_url=config.base_url,
        api_key=config.api_key.get_secret_value(),
        model=config.model,
        target_language=config.target_language,
        timeout_seconds=config.timeout_seconds,
    )
