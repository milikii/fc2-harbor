"""Transmission RPC adapter — spec 15.

Optional second downloader; disabled by default. It implements the same
``Downloader`` protocol as qBittorrent, so the monitor, organizer and task removal
code do not know which downloader produced a job.

Transmission status codes are mapped onto the state names the shared completion
rules understand:

===== ==================== ==================
code  Transmission         mapped state
===== ==================== ==================
0     stopped              pausedDL
1,2   check waiting/running checkingDL
3     queued to download   queuedDL
4     downloading          downloading
5     queued to seed       queuedUP
6     seeding              uploading
===== ==================== ==================

Removal always sends ``delete-local-data: false`` (spec 14.3 applies to every
downloader).
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from app.db.enums import DownloaderName
from app.downloaders.base import (
    DownloaderAuthError,
    DownloaderProtocolError,
    DownloaderRejectedError,
    DownloaderUnavailableError,
)
from app.logging import get_logger
from app.schemas.download import DownloadHandle, DownloadRequest, DownloadSnapshot

_log = get_logger("downloader.transmission")

STATUS_MAP: dict[int, str] = {
    0: "pausedDL",
    1: "checkingDL",
    2: "checkingDL",
    3: "queuedDL",
    4: "downloading",
    5: "queuedUP",
    6: "uploading",
}

FIELDS = [
    "hashString",
    "name",
    "status",
    "percentDone",
    "downloadDir",
    "totalSize",
    "leftUntilDone",
]


class TransmissionClient:
    name = DownloaderName.TRANSMISSION

    def __init__(
        self,
        *,
        base_url: str,
        username: str | None = None,
        password: str | None = None,
        timeout_seconds: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._endpoint = f"{self._base_url}/transmission/rpc"
        self._auth: tuple[str, str] | None = (username, password or "") if username else None
        self._timeout = timeout_seconds
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                # Spec 8.2: downloader traffic is always direct.
                trust_env=False,
                auth=self._auth,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        """Protocol alias (spec 17.3 uses ``close``)."""

        await self.aclose()

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._session_id = None

    async def _rpc(self, method: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"method": method, "arguments": arguments or {}}
        headers = {"X-Transmission-Session-Id": self._session_id} if self._session_id else {}
        started = time.monotonic()
        try:
            response = await self.client.post(self._endpoint, json=payload, headers=headers)
            if response.status_code == 409:
                # Session handshake: the server hands out the id it expects.
                session_id = response.headers.get("X-Transmission-Session-Id")
                if not session_id:
                    raise DownloaderProtocolError("Transmission answered 409 without a session id")
                self._session_id = session_id
                response = await self.client.post(
                    self._endpoint,
                    json=payload,
                    headers={"X-Transmission-Session-Id": session_id},
                )
        except httpx.HTTPError as exc:
            raise DownloaderUnavailableError(
                f"Transmission request failed: {type(exc).__name__}"
            ) from exc

        _log.info(
            "downloader.http",
            downloader=self.name.value,
            method=method,
            status=response.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if response.status_code == 401:
            raise DownloaderAuthError("Transmission rejected the credentials")
        if response.status_code >= 400:
            raise DownloaderProtocolError(
                f"Transmission {method} failed with status {response.status_code}",
                code=f"downloader_http_{response.status_code}",
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DownloaderProtocolError("Transmission returned non-JSON") from exc
        if body.get("result") != "success":
            raise DownloaderProtocolError(
                f"Transmission {method} returned {body.get('result')!r}",
                code="downloader_rejected",
            )
        return body.get("arguments", {})

    # -- protocol -----------------------------------------------------------

    async def add(self, request: DownloadRequest) -> DownloadHandle:
        arguments = {
            "filename": request.magnet,
            "download-dir": request.save_path,
            "paused": request.paused,
            "labels": [request.category],
        }
        body = await self._rpc("torrent-add", arguments)
        added = body.get("torrent-added") or body.get("torrent-duplicate") or {}
        if not added:
            raise DownloaderRejectedError(
                "Transmission did not accept the torrent", code="downloader_add_rejected"
            )
        info_hash = str(added.get("hashString") or request.info_hash).lower()
        _log.info(
            "downloader.add",
            downloader=self.name.value,
            job_id=request.job_id,
            save_path=request.save_path,
            category=request.category,
        )
        return DownloadHandle(downloader=self.name, info_hash=info_hash, task_id=info_hash)

    async def get(self, handle: DownloadHandle) -> DownloadSnapshot | None:
        body = await self._rpc("torrent-get", {"ids": [handle.info_hash], "fields": FIELDS})
        torrents = body.get("torrents") or []
        if not torrents:
            return None
        return self._snapshot(torrents[0])

    async def list_active(self) -> list[DownloadSnapshot]:
        body = await self._rpc("torrent-get", {"fields": FIELDS})
        return [self._snapshot(entry) for entry in body.get("torrents") or []]

    async def remove(self, handle: DownloadHandle, *, delete_files: bool = False) -> None:
        if delete_files:
            raise ValueError("delete_files=True is forbidden; fc2-harbor never deletes payload")
        await self._rpc(
            "torrent-remove",
            {"ids": [handle.info_hash], "delete-local-data": False},
        )
        if await self.get(handle) is not None:
            raise DownloaderProtocolError(
                f"Transmission task {handle.info_hash} still present after remove",
                code="downloader_delete_not_confirmed",
            )
        _log.info(
            "downloader.removed",
            downloader=self.name.value,
            info_hash=handle.info_hash,
            delete_files=False,
        )

    def _snapshot(self, entry: dict[str, Any]) -> DownloadSnapshot:
        info_hash = str(entry.get("hashString") or "").lower()
        status_code = int(entry.get("status") or 0)
        state = STATUS_MAP.get(status_code, "unknown")
        name = entry.get("name")
        download_dir = entry.get("downloadDir")
        content_path = f"{download_dir}/{name}" if download_dir and name else download_dir
        return DownloadSnapshot(
            handle=DownloadHandle(downloader=self.name, info_hash=info_hash, task_id=info_hash),
            state=state,
            progress=float(entry.get("percentDone") or 0.0),
            name=name,
            content_path=content_path,
            save_path=download_dir,
            total_bytes=entry.get("totalSize"),
            downloaded_bytes=(
                int(entry["totalSize"]) - int(entry.get("leftUntilDone") or 0)
                if entry.get("totalSize") is not None
                else None
            ),
            extra={"hash": info_hash, "status": status_code},
        )
