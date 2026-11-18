"""Card publishing — spec 9.1 / 9.2.

For every content that is discovered, not accepted and not yet notified:

1. enrich metadata once (providers + images + translation);
2. send the cover photo with the caption (tag, title, code, Nyaa link);
3. send the full description as follow-up text messages when it does not fit;
4. send the preview images as media groups of 2–10;
5. record every message id in ``notifications``.

Sending is resumable: the notification row exists before the first message, each
step records its ids, and a retry continues from the last recorded step instead of
re-posting the card.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from aiogram.types import InlineKeyboardMarkup
from sqlalchemy import select

from app.bots.keyboards import card_keyboard
from app.config import Settings
from app.db.enums import AssetRole, CoverStatus, MetadataStatus, NotificationStatus
from app.db.models import Asset, Content, Release
from app.db.session import Database
from app.logging import get_logger
from app.providers.coordinator import OFFICIAL_DESCRIPTION_PROVIDER
from app.schemas.release import ReleaseCandidate  # noqa: F401  (typing reference)
from app.services import notifications as notification_store
from app.services.cards import PARSE_MODE, CardSource, build_card_messages
from app.services.metadata import MetadataService
from app.services.push_queue import PushCandidate, find_pending_push_candidates
from app.services.telegram_assets import TelegramAssetUploader, TelegramUploadError
from app.services.translate import text_digest

_log = get_logger("publisher")

MEDIA_GROUP_MAX = 10
MEDIA_GROUP_MIN = 2


@dataclass(slots=True)
class PublishReport:
    published: int = 0
    resumed: int = 0
    skipped: int = 0
    failed: int = 0
    text_only: int = 0
    #: Held back because no cover was available (``publisher.require_cover``).
    deferred: int = 0
    errors: list[str] = field(default_factory=list)


class Publisher:
    def __init__(
        self,
        *,
        db: Database,
        metadata: MetadataService,
        uploader: TelegramAssetUploader,
        bot: object,
        settings: Settings,
        bot_name: str = "subscription",
        metrics: object | None = None,
    ) -> None:
        self.db = db
        self.metadata = metadata
        self.uploader = uploader
        self.bot = bot
        self.settings = settings
        self.bot_name = bot_name
        self.metrics = metrics
        self.require_cover = settings.publisher.require_cover
        self.cover_retry_limit = settings.publisher.cover_retry_limit

    # -- public API ---------------------------------------------------------

    async def publish_once(self, *, limit: int | None = None) -> PublishReport:
        report = PublishReport()
        if limit is None:
            limit = self.settings.subscription.max_push_per_round
        async with self.db.session() as session:
            candidates = await find_pending_push_candidates(session, limit=limit)

        for candidate in candidates:
            try:
                outcome = await self.publish(candidate)
            except Exception as exc:
                report.failed += 1
                report.errors.append(f"{candidate.content_id}: {type(exc).__name__}")
                self._count("cards.failed")
                _log.exception("publisher.card_failed", content_id=candidate.content_id)
                continue
            if outcome == "deferred":
                report.deferred += 1
                self._count("cards.deferred")
            elif outcome == "skipped":
                report.skipped += 1
                self._count("cards.skipped")
            elif outcome == "resumed":
                report.resumed += 1
                self._count("cards.resumed")
            else:
                report.published += 1
                self._count("cards.published")

        if candidates:
            _log.info(
                "publisher.batch",
                candidates=len(candidates),
                published=report.published,
                resumed=report.resumed,
                skipped=report.skipped,
                failed=report.failed,
            )
        return report

    async def publish(self, candidate: PushCandidate) -> str:
        content, release = await self._load(candidate.content_id)
        if content is None:
            return "skipped"

        await self._ensure_metadata(content, need_cover=self.require_cover)
        card = await self._build_card(content, release, candidate)
        cover = await self._cover_asset(content.id)
        if cover is None and self.require_cover:
            # No cover: do not send a degraded card. Retry on a later round until
            # the limit is reached, then stop for good (user-facing quality rule).
            await self._defer_missing_cover(content.id)
            return "deferred"
        await self._mark_cover_ready(content.id, cover)
        previews = await self._preview_assets(content.id)

        outcomes: list[str] = []
        for chat_id in self.settings.telegram.allowed_user_ids:
            outcomes.append(await self._publish_to_chat(chat_id, content.id, card, cover, previews))

        if all(outcome == "skipped" for outcome in outcomes):
            return "skipped"
        if any(outcome == "resumed" for outcome in outcomes):
            return "resumed"
        return "published"

    def _count(self, name: str) -> None:
        if self.metrics is not None:
            self.metrics.inc(name)  # type: ignore[attr-defined]

    # -- per chat -----------------------------------------------------------

    async def _publish_to_chat(
        self,
        chat_id: int,
        content_id: str,
        card: CardSource,
        cover: Asset | None,
        previews: list[Asset],
    ) -> str:
        async with self.db.transaction() as session:
            notification = await notification_store.get_or_create(
                session, content_id=content_id, bot_name=self.bot_name, chat_id=chat_id
            )
            notification_id = notification.id
            already_sent = notification.status is NotificationStatus.SENT
            poster_id = notification.poster_message_id
            description_ids = list(notification.description_message_ids_json or [])
            media_ids = list(notification.media_message_ids_json or [])

        if already_sent:
            return "skipped"

        resumed = poster_id is not None
        messages = build_card_messages(card)
        keyboard = card_keyboard(content_id, card.official_url)

        if poster_id is None:
            poster_id = await self._send_poster(chat_id, card, cover, messages.caption, keyboard)
            async with self.db.transaction() as session:
                await notification_store.record_progress(
                    session, notification_id, poster_message_id=poster_id
                )

        pending_texts = messages.extra_texts[len(description_ids) :]
        for text in pending_texts:
            message = await self.bot.send_message(  # type: ignore[attr-defined]
                chat_id, text, parse_mode=PARSE_MODE
            )
            description_ids.append(message.message_id)
            async with self.db.transaction() as session:
                await notification_store.record_progress(
                    session, notification_id, description_message_ids=[message.message_id]
                )

        if not media_ids and previews:
            media_ids = await self._send_previews(chat_id, previews)
            if media_ids:
                async with self.db.transaction() as session:
                    await notification_store.record_progress(
                        session, notification_id, media_message_ids=media_ids
                    )

        async with self.db.transaction() as session:
            await notification_store.mark_sent(session, notification_id)

        _log.info(
            "publisher.sent",
            content_id=content_id,
            chat_id=chat_id,
            poster_message_id=poster_id,
            descriptions=len(description_ids),
            previews=len(media_ids),
            resumed=resumed,
        )
        return "resumed" if resumed else "published"

    async def _send_poster(
        self,
        chat_id: int,
        card: CardSource,
        cover: Asset | None,
        caption: str,
        reply_markup: InlineKeyboardMarkup,
    ) -> int:
        if cover is not None:
            try:
                sent = await self.uploader.send_photo_message(
                    self.bot,
                    chat_id,
                    cover,
                    caption=caption,
                    reply_markup=reply_markup,
                    parse_mode=PARSE_MODE,
                )
                if sent.message_id is not None:
                    return sent.message_id
            except TelegramUploadError as exc:
                # Missing/broken image must not cost the whole card.
                _log.warning(
                    "publisher.cover_failed", content_id=card.content_id, error_code=exc.code
                )
        message = await self.bot.send_message(  # type: ignore[attr-defined]
            chat_id, caption, reply_markup=reply_markup, parse_mode=PARSE_MODE
        )
        return int(message.message_id)

    async def _send_previews(self, chat_id: int, previews: list[Asset]) -> list[int]:
        from aiogram.types import InputMediaPhoto

        ids: list[int] = []
        media: list[InputMediaPhoto] = []
        for asset in previews:
            try:
                reference = await self.uploader.photo_reference(asset)
            except TelegramUploadError as exc:
                _log.warning("publisher.preview_skipped", asset_id=asset.id, error_code=exc.code)
                continue
            media.append(InputMediaPhoto(media=reference))

        if not media:
            return ids

        if len(media) == 1:
            # Telegram needs 2–10 items for a media group.
            message = await self.bot.send_photo(chat_id, media[0].media)  # type: ignore[attr-defined]
            ids.append(int(message.message_id))
            return ids

        for start in range(0, len(media), MEDIA_GROUP_MAX):
            chunk = media[start : start + MEDIA_GROUP_MAX]
            if len(chunk) < MEDIA_GROUP_MIN:
                message = await self.bot.send_photo(chat_id, chunk[0].media)  # type: ignore[attr-defined]
                ids.append(int(message.message_id))
                continue
            messages = await self.bot.send_media_group(chat_id, media=chunk)  # type: ignore[attr-defined]
            ids.extend(int(message.message_id) for message in messages)
        return ids

    # -- data ---------------------------------------------------------------

    async def _load(self, content_id: str) -> tuple[Content | None, Release | None]:
        async with self.db.session() as session:
            content = await session.get(Content, content_id)
            if content is None:
                return None, None
            release = await session.scalar(
                select(Release)
                .where(Release.content_id == content_id)
                .order_by(Release.published_at.asc().nullslast(), Release.created_at)
                .limit(1)
            )
            return content, release

    async def _ensure_metadata(self, content: Content, *, need_cover: bool = False) -> None:
        """Enrich once, unless a cover is still missing and one is required.

        With ``publisher.require_cover`` a content that has previews but no cover
        must be retried, otherwise it would wait for a cover that never comes.
        """

        async with self.db.session() as session:
            assets = await session.scalar(
                select(Asset.id).where(Asset.content_id == content.id).limit(1)
            )
            has_cover = await session.scalar(
                select(Asset.id)
                .where(Asset.content_id == content.id, Asset.role == AssetRole.COVER)
                .limit(1)
            )
        if has_cover is not None:
            return
        if not need_cover and assets is not None:
            return
        if content.metadata_status is MetadataStatus.FAILED and not need_cover:
            return
        try:
            await self.metadata.enrich(content.id)
        except Exception:
            _log.exception("publisher.enrich_failed", content_id=content.id)

    async def _build_card(
        self, content: Content, release: Release | None, candidate: PushCandidate
    ) -> CardSource:
        nyaa_url = (
            (release.source_url if release else None)
            or candidate.source_url
            or f"https://sukebei.nyaa.si/view/{candidate.source_item_id}"
        )
        # ``📝`` shows the description the enrichment selected and persisted, not
        # only an FC2 body: a trusted provider summary or a cleaned Nyaa plot is
        # valid too. The translation is only shown when its source digest still
        # matches the stored description (a stale translation is hidden).
        description_ja = content.description_ja
        description_source = content.description_source
        description_zh = content.description_zh
        if not description_zh or not description_zh.strip():
            description_zh = None
        elif (
            content.description_zh_source_hash is not None
            and content.description_zh_source_hash != text_digest(description_ja)
        ):
            description_zh = None
        # Both values come from the release row already loaded by ``_load``: no
        # extra query per card.
        size_text = None
        published_at = None
        if release is not None:
            published_at = release.published_at
            payload = release.raw_payload_json or {}
            raw_size = payload.get("size") if isinstance(payload, dict) else None
            size_text = str(raw_size).strip() if raw_size else None
        return CardSource(
            content_id=content.id,
            canonical_key=content.canonical_key,
            display_code=content.display_code or content.canonical_key,
            title=content.title_ja,
            title_zh=content.title_zh,
            description_ja=description_ja,
            description_zh=description_zh,
            description_official=(description_source in (None, OFFICIAL_DESCRIPTION_PROVIDER)),
            censorship=content.censorship,
            nyaa_url=nyaa_url,
            official_url=content.official_url,
            size_text=size_text,
            published_at=published_at,
            timezone=self.settings.app.timezone,
        )

    async def _defer_missing_cover(self, content_id: str) -> None:
        """Count the attempt and stop retrying once the limit is reached."""

        async with self.db.transaction() as session:
            content = await session.get(Content, content_id)
            if content is None:
                return
            if content.cover_status == CoverStatus.GIVE_UP.value:
                return
            content.cover_retry_count = (content.cover_retry_count or 0) + 1
            if content.cover_retry_count >= self.cover_retry_limit:
                content.cover_status = CoverStatus.GIVE_UP.value
                _log.warning(
                    "publisher.cover_give_up",
                    content_id=content_id,
                    attempts=content.cover_retry_count,
                    limit=self.cover_retry_limit,
                )
            else:
                content.cover_status = CoverStatus.PENDING_COVER.value
                _log.info(
                    "publisher.cover_missing",
                    content_id=content_id,
                    attempts=content.cover_retry_count,
                    limit=self.cover_retry_limit,
                )

    async def _mark_cover_ready(self, content_id: str, cover: Asset | None) -> None:
        if cover is None:
            return
        async with self.db.transaction() as session:
            content = await session.get(Content, content_id)
            if content is not None and content.cover_status != CoverStatus.READY.value:
                content.cover_status = CoverStatus.READY.value

    async def _cover_asset(self, content_id: str) -> Asset | None:
        async with self.db.session() as session:
            return await session.scalar(
                select(Asset)
                .where(Asset.content_id == content_id, Asset.role == AssetRole.COVER)
                .order_by(Asset.position)
                .limit(1)
            )

    async def _preview_assets(self, content_id: str) -> list[Asset]:
        async with self.db.session() as session:
            return list(
                (
                    await session.scalars(
                        select(Asset)
                        .where(Asset.content_id == content_id, Asset.role == AssetRole.PREVIEW)
                        .order_by(Asset.position)
                        .limit(self.settings.image_fetch.max_previews)
                    )
                ).all()
            )


async def run_publisher_loop(
    publisher: Publisher,
    *,
    interval_seconds: float,
    stop_event: object,
    batch: int = 5,
) -> None:
    """Push pending cards until shutdown; a failing batch is logged, not fatal."""

    import asyncio

    while not stop_event.is_set():  # type: ignore[attr-defined]
        try:
            await publisher.publish_once(limit=batch)
        except Exception:
            _log.exception("publisher.batch_failed")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)  # type: ignore[attr-defined]
        except TimeoutError:
            continue
