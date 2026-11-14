"""Downloader interface — spec 15 / 17.3.

qBittorrent implements it in M1; Transmission reuses the same protocol in M5.
Adapters never call Telegram, never touch the database and never delete files
unless the caller explicitly asks (which the application never does).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.db.enums import DownloaderName
from app.schemas.download import DownloadHandle, DownloadRequest, DownloadSnapshot


class DownloaderError(RuntimeError):
    """Base class for downloader failures; ``code`` is stable for logs/tests."""

    code = "downloader_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class DownloaderAuthError(DownloaderError):
    code = "downloader_auth_failed"


class DownloaderUnavailableError(DownloaderError):
    code = "downloader_unreachable"


class DownloaderProtocolError(DownloaderError):
    code = "downloader_protocol_error"


class DownloaderRejectedError(DownloaderError):
    code = "downloader_rejected"


@runtime_checkable
class Downloader(Protocol):
    """Spec 17.3 — the only interface the coordinator and monitor use."""

    name: DownloaderName

    async def add(self, request: DownloadRequest) -> DownloadHandle:
        """Add a magnet and return a handle with the real task id."""

    async def get(self, handle: DownloadHandle) -> DownloadSnapshot | None:
        """Return the current snapshot, or ``None`` when the task is gone."""

    async def list_active(self) -> list[DownloadSnapshot]:
        """Return every task the downloader currently knows about."""

    async def remove(self, handle: DownloadHandle, *, delete_files: bool = False) -> None:
        """Remove a task. ``delete_files`` must stay ``False`` (spec 14.3)."""

    async def close(self) -> None:
        """Release network resources."""
