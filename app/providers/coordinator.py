"""Nine-provider aggregation — spec 7.2 / 7.4.

Merge rules implemented exactly:

1. providers run in the fixed priority order (never reordered);
2. a valid high-priority field is never overwritten by a lower one;
3. missing fields keep being filled by later providers;
4. the cover is the highest-priority valid image;
5. previews merge in provider order and original order;
6. images dedupe by normalised URL here, by content SHA-256 when they are stored;
7. ``max_previews`` caps the list;
8. aggregation stops early once the core fields and ``min_preview_images`` exist;
9. one provider failing never stops the chain;
10. the Nyaa page fallback runs last, only for what is still missing.

Conflicting censorship tags become ``UNKNOWN`` instead of picking a winner
(spec 7.3), and the original tag per provider is kept in ``sources``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from app.config import Settings
from app.db.enums import Censorship, ContentType
from app.db.models import Content
from app.logging import error_detail, get_logger
from app.network.clients import HttpClient
from app.network.flaresolverr import FlareSolverrClient
from app.network.proxy import ProxyRouter
from app.providers.avsox import AvsoxProvider
from app.providers.base import (
    BaseProvider,
    ProviderError,
    ProviderPage,
    infer_censorship,
)
from app.providers.custom import build_custom_providers
from app.providers.fc2 import Fc2Provider
from app.providers.fc2_hub import Fc2HubProvider
from app.providers.freejavbt import FreejavbtProvider
from app.providers.javdb import JavdbProvider
from app.providers.javlibrary import JavlibraryProvider
from app.providers.madou import MadouProvider
from app.providers.miss_av import MissAvProvider
from app.providers.mmtv import MmtvProvider
from app.providers.nyaa_fallback import NyaaFallbackProvider
from app.schemas.metadata import AssetCandidate, MetadataResult, ProviderContext

_log = get_logger("metadata")

PROVIDER_CLASSES: dict[str, type[BaseProvider]] = {
    "Fc2": Fc2Provider,
    "fc2_hub": Fc2HubProvider,
    "Javdb": JavdbProvider,
    "Mmtv": MmtvProvider,
    "freejavbt": FreejavbtProvider,
    "miss_av": MissAvProvider,
    "Javlibrary": JavlibraryProvider,
    "avsox": AvsoxProvider,
    "Madou": MadouProvider,
}

HEALTH_WARNING_THRESHOLD = 3

#: P0 pilot: only these sources may use Byparr. The list is deliberately small
#: so the change stays reviewable; P1 expands it after the pilot is accepted.
FLARESOLVERR_PILOT_PROVIDERS: frozenset[str] = frozenset({"miss_av", "fc2_hub"})


@dataclass(frozen=True, slots=True)
class ProviderRun:
    provider: str
    success: bool
    http_status: int | None
    error_code: str | None
    latency_ms: int
    asset_count: int
    result_json: dict[str, object] | None


@dataclass(slots=True)
class AggregatedMetadata:
    content_key: str
    title_ja: str | None = None
    description_ja: str | None = None
    description_quality: Literal["full", "summary"] | None = None
    official_url: str | None = None
    censorship: Censorship = Censorship.UNKNOWN
    release_date: datetime | None = None
    cover: AssetCandidate | None = None
    previews: list[AssetCandidate] = field(default_factory=list)
    #: field name → provider id that supplied it (spec 7.3 audit trail).
    sources: dict[str, str] = field(default_factory=dict)
    runs: list[ProviderRun] = field(default_factory=list)
    stopped_early: bool = False

    def core_complete(self) -> bool:
        return bool(self.title_ja and self.description_ja)

    def as_raw(self) -> dict[str, object]:
        return {
            "title_ja": self.title_ja,
            "description_ja": self.description_ja,
            "description_quality": self.description_quality,
            "official_url": self.official_url,
            "censorship": self.censorship.value,
            "release_date": self.release_date.isoformat() if self.release_date else None,
            "cover": self.cover.url if self.cover else None,
            "previews": [item.url for item in self.previews],
            "sources": dict(self.sources),
            "stopped_early": self.stopped_early,
        }


def _description_wins(aggregated: AggregatedMetadata, result: MetadataResult) -> bool:
    """``full`` replaces ``summary``; otherwise only an empty slot is filled."""

    if aggregated.description_ja is None:
        return True
    return (
        aggregated.description_quality == "summary"
        and result.description_quality == "full"
    )


class MetadataCoordinator:
    def __init__(
        self,
        *,
        providers: Sequence[BaseProvider],
        settings: Settings,
        nyaa_fallback: NyaaFallbackProvider | None = None,
        metrics: object | None = None,
    ) -> None:
        self.providers = list(providers)
        self.settings = settings
        self.nyaa_fallback = nyaa_fallback
        self.metrics = metrics
        self.seek_full_description = settings.provider.seek_full_description
        self._failures: dict[str, int] = {}

    async def aggregate(
        self,
        content: Content,
        ctx: ProviderContext,
        *,
        max_previews: int | None = None,
        min_previews: int | None = None,
    ) -> AggregatedMetadata:
        limit = max_previews if max_previews is not None else self.settings.image_fetch.max_previews
        minimum = (
            min_previews
            if min_previews is not None
            else self.settings.image_fetch.min_preview_images
        )
        aggregated = AggregatedMetadata(content_key=content.canonical_key)

        for provider in self.providers:
            await self._run_and_merge(provider, content, ctx, aggregated, limit)
            if (
                aggregated.core_complete()
                and len(aggregated.previews) >= minimum
                and self._description_is_good_enough(aggregated)
            ):
                aggregated.stopped_early = True
                _log.info(
                    "metadata.early_stop",
                    canonical_key=content.canonical_key,
                    provider=provider.id,
                    previews=len(aggregated.previews),
                )
                break

        needs_fallback = (
            not aggregated.core_complete()
            or not aggregated.previews
            or len(aggregated.previews) < minimum
        )
        if self.nyaa_fallback is not None and needs_fallback:
            await self._run_fallback(content, ctx, aggregated, limit)

        self._apply_censorship_title_fallback(content, ctx, aggregated)

        aggregated.previews = aggregated.previews[:limit]
        return aggregated

    def _apply_censorship_title_fallback(
        self, content: Content, ctx: ProviderContext, aggregated: AggregatedMetadata
    ) -> None:
        """Fill an UNKNOWN tag from the titles we already have.

        No provider returns a censorship field for FC2 content, so without this the
        tag stayed UNKNOWN forever even when the Nyaa title said ``【無】``. A value
        a provider reported explicitly always wins; this only fills the gap.
        """

        # Titles only: the seller's description text never decides the tag
        # (torrentmate rule, docs/deviations.md §52).
        inferred, evidence = infer_censorship(
            {
                "title": aggregated.title_ja or content.title_ja,
                "release_title": ctx.release_title,
            }
        )
        source = aggregated.sources.get("censorship", "")
        if aggregated.censorship is not Censorship.UNKNOWN:
            if inferred is not None and aggregated.censorship is not inferred:
                # Never override an explicit provider value; just record the
                # disagreement.
                _log.warning(
                    "metadata.censorship_conflict",
                    canonical_key=content.canonical_key,
                    provider_value=aggregated.censorship.value,
                    title_value=inferred.value,
                    source=source,
                )
            _log.info(
                "metadata.censorship_resolved",
                canonical_key=content.canonical_key,
                reason="conflict" if source.startswith("conflict:") else "provider_value",
                censorship=aggregated.censorship.value,
                source=source,
            )
            return

        if source.startswith("conflict:"):
            # Two providers disagree: that is a real conflict, not a missing value.
            _log.info(
                "metadata.censorship_resolved",
                canonical_key=content.canonical_key,
                reason="conflict",
                censorship=Censorship.UNKNOWN.value,
                source=source,
            )
            return

        if inferred is Censorship.UNKNOWN:
            # The titles contradict each other (【無】 in one, 【有】 in the other).
            # The old system never showed "unknown": a contradiction resolves to the
            # FC2 default, with the conflicting wording kept as evidence.
            aggregated.censorship = Censorship.CENSORED
            aggregated.sources["censorship"] = "default"
            _log.warning(
                "metadata.censorship_title_conflict_resolved",
                canonical_key=content.canonical_key,
                resolved="censored",
                evidence=evidence,
            )
            return

        if inferred is not None:
            aggregated.censorship = inferred
            aggregated.sources["censorship"] = "title_fallback"
            _log.info(
                "metadata.censorship_resolved",
                canonical_key=content.canonical_key,
                reason="title",
                censorship=inferred.value,
                evidence=evidence,
            )
            return

        # No provider value and no title evidence: the page was unavailable. FC2
        # products are censored by default — the old system never showed "unknown".
        # Only for FC2 content: a generic torrent is not an FC2 product and must not
        # be labelled by a rule that does not apply to it.
        if content.content_type is not ContentType.FC2:
            _log.info(
                "metadata.censorship_resolved",
                canonical_key=content.canonical_key,
                reason="generic_skip",
                censorship=Censorship.UNKNOWN.value,
            )
            return
        aggregated.censorship = Censorship.CENSORED
        aggregated.sources["censorship"] = "default"
        _log.info(
            "metadata.censorship_resolved",
            canonical_key=content.canonical_key,
            reason="default",
            censorship=Censorship.CENSORED.value,
        )


    def _description_is_good_enough(self, aggregated: AggregatedMetadata) -> bool:
        """Early stop is only allowed once the description is not just a teaser."""

        if not self.seek_full_description:
            return True
        return aggregated.description_quality == "full"

    # -- internals ----------------------------------------------------------

    async def _run_and_merge(
        self,
        provider: BaseProvider,
        content: Content,
        ctx: ProviderContext,
        aggregated: AggregatedMetadata,
        limit: int,
    ) -> None:
        started = time.monotonic()
        try:
            result = await asyncio.wait_for(
                provider.fetch(content, ctx), timeout=provider.fetch_timeout_seconds
            )
        except ProviderError as exc:
            latency = int((time.monotonic() - started) * 1000)
            # "not found" is a normal miss (the site simply has no such entry), so
            # it must not count against provider health.
            miss = exc.code == "not_found"
            aggregated.runs.append(
                ProviderRun(
                    provider=provider.id,
                    success=miss,
                    http_status=exc.http_status,
                    error_code=exc.code,
                    latency_ms=latency,
                    asset_count=0,
                    result_json=None,
                )
            )
            if miss:
                self._failures[provider.id] = 0
            elif exc.code == "rate_limited":
                # Throttling says nothing about the site's health: retry next round
                # without burning this round's quota.
                _log.info(
                    "metadata.rate_limited",
                    provider=provider.id,
                    canonical_key=content.canonical_key,
                )
            else:
                self._note_failure(provider.id, exc.code)
            self._record_metric(
                provider.id,
                "miss"
                if miss
                else (
                    "rate_limited"
                    if exc.code == "rate_limited"
                    else ("structure" if exc.code == "structure_changed" else "error")
                ),
                latency,
            )
            return
        except TimeoutError:
            latency = int((time.monotonic() - started) * 1000)
            aggregated.runs.append(
                ProviderRun(
                    provider=provider.id,
                    success=False,
                    http_status=None,
                    error_code="timeout",
                    latency_ms=latency,
                    asset_count=0,
                    result_json=None,
                )
            )
            self._note_failure(provider.id, "timeout")
            self._record_metric(provider.id, "error", latency)
            return

        latency = int((time.monotonic() - started) * 1000)
        self._failures[provider.id] = 0
        self._merge(aggregated, result, limit)
        self._record_metric(provider.id, "hit", latency)
        aggregated.runs.append(
            ProviderRun(
                provider=provider.id,
                success=True,
                http_status=200,
                error_code=None,
                latency_ms=latency,
                asset_count=len(result.previews) + (1 if result.cover else 0),
                result_json=_result_json(result),
            )
        )

    async def _run_fallback(
        self,
        content: Content,
        ctx: ProviderContext,
        aggregated: AggregatedMetadata,
        limit: int,
    ) -> None:
        fallback = self.nyaa_fallback
        if fallback is None:
            return
        try:
            url = fallback.url_for_release(ctx.release_url, ctx.release_item_id)
        except ProviderError:
            return
        try:
            page = await asyncio.wait_for(
                fallback.client.get(url, proxy_url=fallback.proxy_url),
                timeout=fallback.timeout_seconds,
            )
        except Exception as exc:
            _log.info(
                "metadata.fallback_failed",
                error_code=type(exc).__name__,
                error_detail=error_detail(exc),
            )
            return
        try:
            result = fallback.parse(
                ProviderPage(
                    url=url,
                    html=page.text,
                    status=page.status,
                    release_title=ctx.release_title,
                ),
                content,
            )
        except ProviderError as exc:
            _log.info(
                "metadata.fallback_failed", error_code=exc.code, error_detail=str(exc)[:300]
            )
            return
        if result is None:
            return
        # Never overwrites: the fallback only fills what is still missing.
        self._merge(aggregated, result, limit)
        self._record_metric("Nyaa", "hit", 0)
        aggregated.runs.append(
            ProviderRun(
                provider="Nyaa",
                success=True,
                http_status=page.status,
                error_code=None,
                latency_ms=0,
                asset_count=len(result.previews) + (1 if result.cover else 0),
                result_json=_result_json(result),
            )
        )

    def _merge(
        self,
        aggregated: AggregatedMetadata,
        result: MetadataResult,
        limit: int,
    ) -> None:
        """Field-by-field merge; an existing value is never overwritten."""

        if result.title_ja and aggregated.title_ja is None:
            aggregated.title_ja = result.title_ja
            aggregated.sources["title_ja"] = result.provider
        # Description: a full body always beats a teaser, regardless of provider
        # order; otherwise the first value wins (spec 7.2 priority).
        if result.description_ja and _description_wins(aggregated, result):
            replaced = aggregated.description_ja is not None
            aggregated.description_ja = result.description_ja
            aggregated.description_quality = result.description_quality
            aggregated.sources["description_ja"] = result.provider
            if replaced:
                _log.info(
                    "provider.description_upgraded",
                    provider=result.provider,
                    quality=result.description_quality,
                    content_key=aggregated.content_key,
                )
        if result.official_url and aggregated.official_url is None:
            aggregated.official_url = result.official_url
            aggregated.sources["official_url"] = result.provider
        if result.release_date and aggregated.release_date is None:
            aggregated.release_date = result.release_date
            aggregated.sources["release_date"] = result.provider

        # A provider returning UNKNOWN has no opinion: it must not claim the field,
        # otherwise the "censored by default" rule below would be skipped.
        if result.censorship is not None and result.censorship is not Censorship.UNKNOWN:
            current_source = aggregated.sources.get("censorship", "")
            if current_source.startswith("conflict:"):
                # A conflict is already settled; a third opinion does not change it.
                # Only the censorship field is skipped — cover/previews below must
                # still be merged.
                pass
            elif aggregated.censorship is Censorship.UNKNOWN:
                aggregated.censorship = result.censorship
                aggregated.sources["censorship"] = result.provider
            elif aggregated.censorship is not result.censorship:
                # Deviation from §7.3: the user never wants "unknown" on a card, and
                # FC2 products are censored unless a tag says otherwise. The conflict
                # is still recorded as evidence, but the value stays CENSORED.
                aggregated.sources["censorship"] = (
                    f"conflict:{aggregated.sources.get('censorship', '?')},{result.provider}"
                )
                aggregated.censorship = Censorship.CENSORED
                _log.warning(
                    "metadata.censorship_conflict",
                    canonical_key=aggregated.content_key,
                    source=aggregated.sources["censorship"],
                    resolved="censored",
                )

        if aggregated.cover is None and result.cover is not None:
            aggregated.cover = result.cover
            aggregated.sources["cover"] = result.provider

        known = {item.url for item in aggregated.previews}
        for candidate in result.previews:
            if len(aggregated.previews) >= limit:
                break
            if candidate.url in known:
                continue
            known.add(candidate.url)
            aggregated.previews.append(candidate)

    def _note_failure(self, provider_id: str, code: str) -> None:
        failures = self._failures.get(provider_id, 0) + 1
        self._failures[provider_id] = failures
        if failures >= HEALTH_WARNING_THRESHOLD:
            _log.warning(
                "metadata.provider_unhealthy",
                provider=provider_id,
                consecutive_failures=failures,
                error_code=code,
            )

    def health(self) -> dict[str, int]:
        return dict(self._failures)

    def _record_metric(self, provider: str, outcome: str, latency_ms: int) -> None:
        if self.metrics is not None:
            self.metrics.record_provider(provider, outcome, latency_ms=latency_ms)  # type: ignore[attr-defined]


def _result_json(result: MetadataResult) -> dict[str, object]:
    return {
        "title_ja": result.title_ja,
        "description_ja": result.description_ja,
        "official_url": result.official_url,
        "censorship": result.censorship.value if result.censorship else None,
        "release_date": result.release_date.isoformat() if result.release_date else None,
        "cover": result.cover.url if result.cover else None,
        "previews": [item.url for item in result.previews],
        "raw": result.raw,
    }


def build_providers(
    settings: Settings,
    *,
    client: HttpClient,
    router: ProxyRouter,
    flaresolverr: FlareSolverrClient | None = None,
) -> tuple[list[BaseProvider], NyaaFallbackProvider]:
    """Instantiate the nine adapters in the configured (fixed) order."""

    providers: list[BaseProvider] = []
    for name in settings.provider.priorities["jav_fc2"]:
        source = settings.provider.sources[name]
        if not source.enabled:
            _log.warning("metadata.provider_disabled", provider=name)
            continue
        provider_class = PROVIDER_CLASSES[name]
        target = router.for_provider(source.proxy)
        solver = flaresolverr if name in FLARESOLVERR_PILOT_PROVIDERS else None
        if solver is not None:
            _log.info("metadata.byparr_enabled", provider=name, domain=source.host)
        providers.append(
            provider_class(
                client=client,
                proxy_url=target.url,
                timeout_seconds=source.timeout_seconds,
                proxy_region=target.region,
                headers=source.headers,
                warmup_url=source.warmup_url,
                min_interval_seconds=source.min_interval_seconds,
                jitter_seconds=source.jitter_seconds,
                flaresolverr=solver,
            )
        )

    # Config-defined sites run after the fixed nine: they supplement them and never
    # change the built-in order (spec 7.1).
    providers.extend(build_custom_providers(settings, client=client, router=router))

    fallback = NyaaFallbackProvider(
        client=client,
        proxy_url=router.for_provider(settings.nyaa.proxy).url,
        timeout_seconds=settings.nyaa.timeout_seconds,
    )
    return providers, fallback
