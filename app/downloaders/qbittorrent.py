"""qBittorrent Web API v2 adapter — spec 14.

Hard rules encoded here:

* ``add`` sets an explicit ``savepath`` and disables AutoTMM, so qBittorrent
  cannot move the task to a different path behind our back (spec 14.1).
* ``remove`` always sends ``deleteFiles=false``; passing ``delete_files=True``
  raises instead of forwarding it (spec 14.3, acceptance list).
* Every call is authenticated through the session cookie and re-authenticates
  once on 403. Credentials never appear in logs.
* ``add`` is idempotent: if qBittorrent already knows the hash the add counts as
  success, which makes a crash between the HTTP call and the database update
  harmless (at-least-once + idempotent side effect).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

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

_log = get_logger("downloader.qbittorrent")


class QBittorrentClient:
    """Minimal, explicit qBittorrent Web API client."""

    name = DownloaderName.QBITTORRENT

    def __init__(
        self,
        *,
        base_url: str,
        username: str,
        password: str,
        timeout_seconds: float = 15.0,
        verify_tls: bool = True,
        confirm_attempts: int = 10,
        confirm_interval_seconds: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout_seconds
        self._verify_tls = verify_tls
        self._confirm_attempts = max(1, confirm_attempts)
        self._confirm_interval = max(0.0, confirm_interval_seconds)
        self._transport = transport
        self._client: httpx.AsyncClient | None = None
        self._logged_in = False
        self._domain = urlsplit(self._base_url).hostname or "unknown"

    # -- lifecycle ----------------------------------------------------------

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(self._timeout),
                verify=self._verify_tls,
                follow_redirects=False,
                # Spec 8.2: never let a global HTTP_PROXY/ALL_PROXY reach the
                # NAS-local downloader. Proxy routing is explicit only.
                trust_env=False,
                transport=self._transport,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._logged_in = False

    # -- auth ---------------------------------------------------------------

    async def login(self, *, force: bool = False) -> None:
        if self._logged_in and not force:
            return
        if force:
            self.client.cookies.clear()
            self._logged_in = False

        started = time.monotonic()
        try:
            response = await self.client.post(
                "/api/v2/auth/login",
                data={"username": self._username, "password": self._password},
                headers={"Referer": self._base_url},
            )
        except httpx.HTTPError as exc:
            raise DownloaderUnavailableError(
                f"qBittorrent login failed: {type(exc).__name__}", code="downloader_unreachable"
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        body = (response.text or "").strip().lower()
        _log.info(
            "downloader.login",
            downloader=self.name.value,
            domain=self._domain,
            status=response.status_code,
            latency_ms=latency_ms,
        )
        if response.status_code == 403:
            raise DownloaderAuthError(
                "qBittorrent refused the login (403, IP may be banned)", code="downloader_banned"
            )
        if response.status_code >= 400 or body != "ok.":
            raise DownloaderAuthError(f"qBittorrent login rejected (status={response.status_code})")
        self._logged_in = True

    # -- HTTP ---------------------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
    ) -> httpx.Response:
        await self.login()
        started = time.monotonic()
        try:
            response = await self.client.request(method, path, params=params, data=data)
            if response.status_code == 403:
                # Session cookie expired: re-authenticate exactly once.
                await self.login(force=True)
                response = await self.client.request(method, path, params=params, data=data)
        except httpx.HTTPError as exc:
            raise DownloaderUnavailableError(
                f"qBittorrent request failed: {type(exc).__name__}"
            ) from exc

        _log.info(
            "downloader.http",
            downloader=self.name.value,
            domain=self._domain,
            method=method,
            path=path.split("?")[0],
            status=response.status_code,
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if response.status_code >= 400:
            raise DownloaderProtocolError(
                f"qBittorrent {method} {path} failed with status {response.status_code}",
                code=f"downloader_http_{response.status_code}",
            )
        return response

    # -- operations ---------------------------------------------------------

    async def add(self, request: DownloadRequest) -> DownloadHandle:
        payload = {
            "urls": request.magnet,
            "savepath": request.save_path,
            "category": request.category,
            "tags": request.tag,
            "autoTMM": "false",
            "paused": "true" if request.paused else "false",
            "sequentialDownload": "false",
            "firstLastPiecePrio": "false",
        }
        response = await self._request("POST", "/api/v2/torrents/add", data=payload)
        body = (response.text or "").strip().lower()
        _log.info(
            "downloader.add",
            downloader=self.name.value,
            job_id=request.job_id,
            save_path=request.save_path,
            category=request.category,
            accepted=body == "ok.",
        )

        handle = DownloadHandle(
            downloader=self.name,
            info_hash=request.info_hash,
            task_id=request.info_hash,
        )
        snapshot = await self._wait_for_torrent(request.info_hash)
        if snapshot is None:
            if body == "ok.":
                # qBittorrent accepted the add but the task is not visible yet;
                # the handle is still valid and the monitor will pick it up.
                return handle
            raise DownloaderRejectedError(
                "qBittorrent rejected the torrent and it is not present",
                code="downloader_add_rejected",
            )
        task_id = snapshot.extra.get("hash") if snapshot.extra else None
        return DownloadHandle(
            downloader=self.name,
            info_hash=snapshot.handle.info_hash,
            task_id=task_id if isinstance(task_id, str) else request.info_hash,
        )

    async def _wait_for_torrent(self, info_hash: str) -> DownloadSnapshot | None:
        for attempt in range(self._confirm_attempts):
            snapshot = await self.get(
                DownloadHandle(downloader=self.name, info_hash=info_hash, task_id=info_hash)
            )
            if snapshot is not None:
                return snapshot
            if attempt < self._confirm_attempts - 1 and self._confirm_interval:
                await asyncio.sleep(self._confirm_interval)
        return None

    async def get(self, handle: DownloadHandle) -> DownloadSnapshot | None:
        response = await self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": handle.info_hash}
        )
        try:
            entries = response.json()
        except ValueError as exc:
            raise DownloaderProtocolError(
                "qBittorrent returned non-JSON for torrents/info"
            ) from exc
        if not isinstance(entries, list) or not entries:
            return None
        return self._snapshot(entries[0])

    async def list_active(self) -> list[DownloadSnapshot]:
        response = await self._request("GET", "/api/v2/torrents/info")
        try:
            entries = response.json()
        except ValueError as exc:
            raise DownloaderProtocolError(
                "qBittorrent returned non-JSON for torrents/info"
            ) from exc
        if not isinstance(entries, list):
            return []
        return [self._snapshot(entry) for entry in entries if isinstance(entry, dict)]

    async def remove(self, handle: DownloadHandle, *, delete_files: bool = False) -> None:
        if delete_files:
            # Spec 14.3 / 24: the application never deletes torrent data.
            raise ValueError("delete_files=True is forbidden; fc2-harbor never deletes payload")
        await self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": handle.info_hash, "deleteFiles": "false"},
        )
        for attempt in range(self._confirm_attempts):
            if await self.get(handle) is None:
                _log.info(
                    "downloader.removed",
                    downloader=self.name.value,
                    info_hash=handle.info_hash,
                    delete_files=False,
                )
                return
            if attempt < self._confirm_attempts - 1 and self._confirm_interval:
                await asyncio.sleep(self._confirm_interval)
        raise DownloaderProtocolError(
            f"qBittorrent task {handle.info_hash} still present after delete",
            code="downloader_delete_not_confirmed",
        )

    # -- mapping ------------------------------------------------------------

    def _snapshot(self, entry: dict[str, Any]) -> DownloadSnapshot:
        info_hash = str(entry.get("hash") or entry.get("infohash") or "").lower()
        total = entry.get("total_size", entry.get("size"))
        completed = entry.get("completed", entry.get("downloaded"))
        return DownloadSnapshot(
            handle=DownloadHandle(
                downloader=self.name,
                info_hash=info_hash,
                task_id=str(entry.get("hash") or info_hash),
            ),
            state=str(entry.get("state") or ""),
            progress=float(entry.get("progress") or 0.0),
            name=entry.get("name"),
            content_path=entry.get("content_path") or None,
            save_path=entry.get("save_path") or None,
            total_bytes=int(total) if isinstance(total, (int, float)) else None,
            downloaded_bytes=int(completed) if isinstance(completed, (int, float)) else None,
            extra={
                "hash": info_hash,
                "category": entry.get("category"),
                "tags": entry.get("tags"),
                "amount_left": entry.get("amount_left"),
            },
        )
