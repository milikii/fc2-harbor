"""Telegram file_id cache and photo sending — spec 8.4 step 6 / 9.1.

Images are uploaded by the backend, never linked to Telegram. Once uploaded, the
resulting ``file_id`` is cached per bot and per content hash, so a second card with
the same image costs no upload at all.

M4 uses :meth:`TelegramAssetUploader.send_photo` for the card: it reuses the cached
file_id when available, otherwise uploads the local file and remembers the id.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from aiogram.types import FSInputFile, InlineKeyboardMarkup
from sqlalchemy import select

from app.db.models import Asset
from app.db.session import Database
from app.logging import get_logger

_log = get_logger("telegram.assets")


@dataclass(frozen=True, slots=True)
class SentPhoto:
    file_id: str
    message_id: int | None


class TelegramUploadError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class TelegramAssetUploader:
    def __init__(self, *, db: Database, bot_name: str = "subscription") -> None:
        self.db = db
        self.bot_name = bot_name

    async def cached_file_id(self, sha256: str) -> str | None:
        """Reuse an upload of the same bytes for this bot (spec 8.4 step 6)."""

        if not sha256:
            return None
        async with self.db.session() as session:
            return await session.scalar(
                select(Asset.telegram_file_id)
                .where(
                    Asset.sha256 == sha256,
                    Asset.telegram_bot == self.bot_name,
                    Asset.telegram_file_id.is_not(None),
                )
                .limit(1)
            )

    async def remember_file_id(self, asset_id: str, file_id: str) -> None:
        async with self.db.transaction() as session:
            asset = await session.get(Asset, asset_id)
            if asset is None:
                return
            asset.telegram_file_id = file_id
            asset.telegram_bot = self.bot_name

    async def send_photo(
        self,
        bot: object,
        chat_id: int,
        asset: Asset,
        *,
        caption: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> str:
        """Send one asset and return its file_id (cached when possible)."""

        sent = await self.send_photo_message(
            bot,
            chat_id,
            asset,
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        return sent.file_id

    async def send_photo_message(
        self,
        bot: object,
        chat_id: int,
        asset: Asset,
        *,
        caption: str | None = None,
        reply_markup: InlineKeyboardMarkup | None = None,
        parse_mode: str | None = None,
    ) -> SentPhoto:
        """Send one asset, reusing a cached file_id, and return the message id.

        A missing local file is an error: the asset row must point at something the
        container can read.
        """

        cached = await self.cached_file_id(asset.sha256 or "")
        if cached:
            message = await bot.send_photo(  # type: ignore[attr-defined]
                chat_id,
                cached,
                caption=caption,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            return SentPhoto(file_id=cached, message_id=getattr(message, "message_id", None))

        path = Path(asset.local_path or "")
        # A single stat is negligible next to a network upload.
        if not asset.local_path or not path.is_file():  # noqa: ASYNC240
            raise TelegramUploadError(
                "missing_file", f"asset {asset.id} has no readable local file"
            )
        message = await bot.send_photo(  # type: ignore[attr-defined]
            chat_id,
            FSInputFile(str(path)),
            caption=caption,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
        )
        file_id = _largest_photo_id(message)
        if not file_id:
            raise TelegramUploadError(
                "no_photo", f"Telegram returned no photo for asset {asset.id}"
            )
        await self.remember_file_id(asset.id, file_id)
        _log.info(
            "telegram.asset_uploaded",
            asset_id=asset.id,
            bot=self.bot_name,
            size_bytes=asset.size_bytes,
        )
        return SentPhoto(file_id=file_id, message_id=getattr(message, "message_id", None))

    async def photo_reference(self, asset: Asset) -> str | FSInputFile:
        """A file_id string when already uploaded, otherwise a local file input."""

        cached = await self.cached_file_id(asset.sha256 or "")
        if cached:
            return cached
        path = Path(asset.local_path or "")
        if not asset.local_path or not path.is_file():  # noqa: ASYNC240
            raise TelegramUploadError(
                "missing_file", f"asset {asset.id} has no readable local file"
            )
        return FSInputFile(str(path))


def _largest_photo_id(message: object) -> str | None:
    photos = getattr(message, "photo", None) or []
    if not photos:
        return None
    return getattr(photos[-1], "file_id", None)
