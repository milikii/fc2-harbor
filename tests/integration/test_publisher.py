"""Card publishing — spec 9.1 / 9.2 (cover, description overflow, media groups).

Fake bot, real database: asserts what is sent, in what order, and that every message
id is recorded so the group can be deleted and a retry can resume.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from app.config import Settings
from app.db.enums import AssetRole, ContentType, NotificationStatus
from app.db.models import Asset, Content, Notification, Release
from app.db.session import Database
from app.network.image_fetcher import ImageFetcher
from app.network.proxy import ProxyRouter
from app.providers.coordinator import MetadataCoordinator
from app.services import notifications as notification_store
from app.services.metadata import MetadataService
from app.services.publisher import Publisher
from app.services.telegram_assets import TelegramAssetUploader
from PIL import Image
from sqlalchemy import select

from tests.support import hex_hash, make_settings

HASH = hex_hash(1001)


class FakeBot:
    def __init__(self) -> None:
        self.counter = 100
        self.photos: list[tuple[int, object, str | None, int]] = []
        self.messages: list[tuple[int, str, int]] = []
        self.groups: list[tuple[int, list[object], list[int]]] = []

    def _next(self) -> int:
        self.counter += 1
        return self.counter

    async def send_photo(self, chat_id: int, photo: object, caption: str | None = None):
        message_id = self._next()
        self.photos.append((chat_id, photo, caption, message_id))
        return SimpleNamespace(
            message_id=message_id, photo=[SimpleNamespace(file_id=f"F{message_id}")]
        )

    async def send_message(self, chat_id: int, text: str):
        message_id = self._next()
        self.messages.append((chat_id, text, message_id))
        return SimpleNamespace(message_id=message_id)

    async def send_media_group(self, chat_id: int, media: list[object]):
        ids = [self._next() for _ in media]
        self.groups.append((chat_id, media, ids))
        return [SimpleNamespace(message_id=message_id) for message_id in ids]


def write_png(path: Path, seed: int) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (10 + seed % 3, 8), (seed % 255, 40, 90)).save(buffer, format="PNG")
    body = buffer.getvalue()
    path.write_bytes(body)
    return body


async def seed(db: Database, tmp_path: Path, *, previews: int = 2, cover: bool = True) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
            title_ja="サンプル作品",
            description_ja="作品の説明です。",
            description_zh="作品说明。",
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

        assets: list[Asset] = []
        if cover:
            assets.append(_asset(content.id, AssetRole.COVER, 0, tmp_path, seed=1))
        for index in range(previews):
            assets.append(_asset(content.id, AssetRole.PREVIEW, index, tmp_path, seed=10 + index))
        session.add_all(assets)
        return content.id


def _asset(content_id: str, role: AssetRole, position: int, tmp_path: Path, *, seed: int) -> Asset:
    path = tmp_path / f"{role.value}-{position}.png"
    body = write_png(path, seed)
    return Asset(
        content_id=content_id,
        provider="Fc2",
        role=role,
        original_url=f"https://img.example.com/{role.value}{position}.png",
        normalized_url=f"https://img.example.com/{role.value}{position}.png",
        sha256=hashlib.sha256(body).hexdigest(),
        local_path=str(path),
        size_bytes=len(body),
        width=10,
        height=8,
        position=position,
    )


def build_publisher(db: Database, settings: Settings, bot: FakeBot, tmp_path: Path) -> Publisher:
    coordinator = MetadataCoordinator(providers=[], settings=settings)
    fetcher = ImageFetcher(cache_dir=tmp_path / "cache", allowed_hosts=["img.example.com"])
    metadata = MetadataService(
        db=db,
        coordinator=coordinator,
        fetcher=fetcher,
        settings=settings,
        router=ProxyRouter(settings.proxy_profiles),
    )
    return Publisher(
        db=db,
        metadata=metadata,
        uploader=TelegramAssetUploader(db=db, bot_name="subscription"),
        bot=bot,
        settings=settings,
    )


async def test_card_with_cover_and_previews(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    content_id = await seed(db, tmp_path)
    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)

    report = await publisher.publish_once()

    assert report.published == 1
    assert len(bot.photos) == 1
    chat_id, _photo, caption, _poster_id = bot.photos[0]
    assert chat_id == settings.telegram.allowed_user_ids[0]
    assert caption and "FC2-PPV-1234567" in caption
    assert caption and "作品の説明です。" in caption
    assert len(bot.groups) == 1
    assert len(bot.groups[0][1]) == 2

    async with db.session() as session:
        notification = await session.scalar(
            select(Notification).where(Notification.content_id == content_id)
        )
    assert notification is not None
    assert notification.status is NotificationStatus.SENT
    assert notification.poster_message_id == bot.photos[0][3]
    assert notification.media_message_ids_json == bot.groups[0][2]


async def test_second_run_does_not_duplicate(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    await seed(db, tmp_path)
    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)

    await publisher.publish_once()
    second = await publisher.publish_once()

    assert second.published == 0
    assert second.skipped == 0  # the content is no longer in the pending query
    assert len(bot.photos) == 1
    assert len(bot.groups) == 1


async def test_text_only_card_without_cover(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    await seed(db, tmp_path, cover=False, previews=0)
    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)

    report = await publisher.publish_once()

    assert report.published == 1
    assert bot.photos == []
    assert len(bot.messages) == 1
    assert "FC2-PPV-1234567" in bot.messages[0][1]


async def test_single_preview_is_sent_as_one_photo(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    await seed(db, tmp_path, previews=1)
    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)

    await publisher.publish_once()

    assert bot.groups == []
    # cover photo + the single preview photo
    assert len(bot.photos) == 2


async def test_long_description_goes_out_as_extra_messages(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    content_id = await seed(db, tmp_path)
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.description_ja = "あ" * 2000
        content.description_zh = "中" * 2000

    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)
    await publisher.publish_once()

    assert bot.messages  # at least one follow-up text message
    assert all(len(text) <= 4096 for _chat, text, _id in bot.messages)
    caption = bot.photos[0][2]
    assert caption is not None and len(caption) <= 1024
    assert "FC2-PPV-1234567" in caption

    async with db.session() as session:
        notification = await session.scalar(
            select(Notification).where(Notification.content_id == content_id)
        )
    assert notification is not None
    assert notification.description_message_ids_json


async def test_push_batch_is_capped_per_round(db: Database, tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path,
        telegram={"allowed_user_ids": "111"},
        subscription={"max_push_per_round": 1},
    )
    await seed(db, tmp_path)
    async with db.transaction() as session:
        extra = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:7654321",
            display_code="FC2-PPV-7654321",
            title_ja="二件目",
            description_ja="説明。",
        )
        session.add(extra)
        await session.flush()
        session.add(
            Release(
                content_id=extra.id,
                source="nyaa",
                source_item_id="item-2",
                source_url="https://sukebei.nyaa.si/view/2",
                magnet=f"magnet:?xt=urn:btih:{hex_hash(2002)}",
                info_hash=hex_hash(2002),
                published_at=datetime.now(UTC),
            )
        )

    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)
    first = await publisher.publish_once()

    assert first.published == 1  # capped by subscription.max_push_per_round

    second = await publisher.publish_once()
    assert second.published == 1  # the backlog drains on the next round


async def test_resume_after_partial_send(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"allowed_user_ids": "111"})
    content_id = await seed(db, tmp_path)
    async with db.transaction() as session:
        notification = await notification_store.get_or_create(
            session,
            content_id=content_id,
            bot_name="subscription",
            chat_id=settings.telegram.allowed_user_ids[0],
        )
        # A crash after the poster: the caption is already posted.
        await notification_store.record_progress(session, notification.id, poster_message_id=999)

    bot = FakeBot()
    publisher = build_publisher(db, settings, bot, tmp_path)
    report = await publisher.publish_once()

    assert report.resumed == 1
    assert bot.photos == []  # no second poster
    assert len(bot.groups) == 1

    async with db.session() as session:
        notification = await session.scalar(
            select(Notification).where(Notification.content_id == content_id)
        )
    assert notification is not None
    assert notification.poster_message_id == 999
    assert notification.status is NotificationStatus.SENT
