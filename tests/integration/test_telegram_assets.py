"""Telegram asset uploads — spec 8.4 step 6 (file_id cache per bot and hash)."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.db.enums import AssetRole, ContentType
from app.db.models import Asset, Content
from app.db.session import Database
from app.services.telegram_assets import TelegramAssetUploader, TelegramUploadError
from PIL import Image
from sqlalchemy import select


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[int, object, str | None]] = []

    async def send_photo(self, chat_id: int, photo: object, caption: str | None = None):
        self.calls.append((chat_id, photo, caption))
        return SimpleNamespace(photo=[SimpleNamespace(file_id="FILE-ABC")])


def write_png(path: Path) -> None:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), (1, 2, 3)).save(buffer, format="PNG")
    path.write_bytes(buffer.getvalue())


async def seed_asset(db: Database, tmp_path: Path, *, sha: str = "a" * 64) -> str:
    image_path = tmp_path / "img.png"
    write_png(image_path)
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
        )
        session.add(content)
        await session.flush()
        asset = Asset(
            content_id=content.id,
            provider="Fc2",
            role=AssetRole.COVER,
            original_url="https://img.example.com/c.jpg",
            normalized_url="https://img.example.com/c.jpg",
            sha256=sha,
            local_path=str(image_path),
            size_bytes=image_path.stat().st_size,
            position=0,
        )
        session.add(asset)
        await session.flush()
        return asset.id


async def test_first_upload_stores_file_id(db: Database, tmp_path: Path) -> None:
    asset_id = await seed_asset(db, tmp_path)
    uploader = TelegramAssetUploader(db=db)
    bot = FakeBot()

    async with db.session() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        file_id = await uploader.send_photo(bot, 111, asset, caption="caption")

    assert file_id == "FILE-ABC"
    assert len(bot.calls) == 1
    chat_id, photo, caption = bot.calls[0]
    assert chat_id == 111 and caption == "caption"
    assert not isinstance(photo, str)  # uploaded the local file

    async with db.session() as session:
        stored = await session.get(Asset, asset_id)
    assert stored is not None
    assert stored.telegram_file_id == "FILE-ABC"
    assert stored.telegram_bot == "subscription"


async def test_second_upload_reuses_cached_file_id(db: Database, tmp_path: Path) -> None:
    asset_id = await seed_asset(db, tmp_path)
    uploader = TelegramAssetUploader(db=db)
    bot = FakeBot()

    async with db.session() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        await uploader.send_photo(bot, 111, asset)
        await uploader.send_photo(bot, 111, asset)

    assert len(bot.calls) == 2
    assert bot.calls[1][1] == "FILE-ABC"  # sent by file_id, not re-uploaded


async def test_cache_is_per_bot(db: Database, tmp_path: Path) -> None:
    asset_id = await seed_asset(db, tmp_path)
    uploader = TelegramAssetUploader(db=db, bot_name="subscription")
    other_bot = FakeBot()

    async with db.session() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        await uploader.send_photo(FakeBot(), 111, asset)
        # A different bot must upload again (file_ids are bot-scoped).
        await TelegramAssetUploader(db=db, bot_name="manual").send_photo(other_bot, 222, asset)

    assert len(other_bot.calls) == 1
    assert not isinstance(other_bot.calls[0][1], str)


async def test_missing_local_file_is_an_error(db: Database, tmp_path: Path) -> None:
    asset_id = await seed_asset(db, tmp_path)
    uploader = TelegramAssetUploader(db=db)

    async with db.transaction() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        Path(asset.local_path or "").unlink()  # noqa: ASYNC240

    async with db.session() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        with pytest.raises(TelegramUploadError) as excinfo:
            await uploader.send_photo(FakeBot(), 111, asset)
    assert excinfo.value.code == "missing_file"


async def test_asset_without_hash_still_uploads(db: Database, tmp_path: Path) -> None:
    asset_id = await seed_asset(db, tmp_path)
    async with db.transaction() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        asset.sha256 = None

    uploader = TelegramAssetUploader(db=db)
    bot = FakeBot()
    async with db.session() as session:
        asset = await session.get(Asset, asset_id)
        assert asset is not None
        file_id = await uploader.send_photo(bot, 111, asset)

    assert file_id == "FILE-ABC"
    async with db.session() as session:
        rows = list((await session.scalars(select(Asset))).all())
    assert len(rows) == 1
