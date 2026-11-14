"""Shared test support: configuration, fakes and small builders.

Nothing here touches the network: qBittorrent and Telegram are fakes, and all
paths live under pytest's tmp_path.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from app.config import (
    CANONICAL_FC2_PROVIDER_ORDER,
    CANONICAL_PROVIDER_HOSTS,
    CATEGORIES,
    Settings,
)
from app.db.enums import ContentType, DownloaderName
from app.downloaders.base import DownloaderProtocolError, DownloaderRejectedError
from app.schemas.download import DownloadHandle, DownloadRequest, DownloadSnapshot
from app.services.gate import GateSubject


def deep_update(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: (value.copy() if isinstance(value, dict) else value) for key, value in base.items()
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_update(result[key], value)
        else:
            result[key] = value
    return result


def make_config(tmp_path: Path) -> dict[str, Any]:
    downloads = tmp_path / "downloads"
    library = tmp_path / "library"
    return {
        "app": {
            "timezone": "Asia/Shanghai",
            "database_url": f"sqlite+aiosqlite:///{tmp_path / 'app.db'}",
            "log_level": "INFO",
            "log_json": False,
            "health_port": 8099,
        },
        "telegram": {
            "manual_bot_token": "123456:TEST-MANUAL-TOKEN",
            "allowed_user_ids": "111,222",
            "proxy": "direct",
        },
        "nyaa": {
            "base_url": "https://sukebei.nyaa.si",
            "query": "FC2",
            "poll_interval_seconds": 180,
            "proxy": "direct",
        },
        "provider": {
            "priorities": {"jav_fc2": list(CANONICAL_FC2_PROVIDER_ORDER)},
            "sources": {
                name: {"host": host, "enabled": True, "proxy": "japan"}
                for name, host in CANONICAL_PROVIDER_HOSTS.items()
            },
        },
        "proxy_profiles": {
            "direct": {"type": "direct"},
            "japan": {
                "type": "socks5h",
                "url": "socks5h://user:password@127.0.0.1:1080",
                "region": "JP",
            },
            "japan_backup": {
                "type": "http",
                "url": "http://127.0.0.1:8081",
                "region": "JP",
            },
        },
        "image_fetch": {
            "default_proxy": "inherit_provider",
            "max_previews": 9,
            "min_preview_images": 3,
        },
        "translation": {"enabled": False},
        "download": {
            "default_downloader": "qbittorrent",
            "routes": dict.fromkeys(CATEGORIES, "qbittorrent"),
        },
        "downloaders": {
            "qbittorrent": {
                "enabled": True,
                "url": "http://127.0.0.1:8080",
                "username": "admin",
                "password": "adminadmin",
                "save_path_template": str(downloads / "{category}" / "{job_id}"),
                "category": "fc2-harbor",
                "poll_interval_seconds": 5,
                "stability_interval_seconds": 0.01,
            },
            "transmission": {"enabled": False},
        },
        "storage": {
            "path_mappings": [{"remote": str(downloads), "local": str(downloads)}],
            "final_roots": {category: str(library / category) for category in CATEGORIES},
            "dir_name_max_length": 120,
        },
    }


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    config = deep_update(make_config(tmp_path), overrides)
    return Settings.from_mapping(config, env={})


def make_subject(
    *,
    canonical_key: str = "fc2_ppv:1234567",
    info_hash: str | None = None,
    source: str = "manual",
    source_item_id: str | None = None,
    display_code: str = "FC2-PPV-1234567",
    content_type: ContentType = ContentType.FC2,
    magnet: str | None = None,
) -> GateSubject:
    return GateSubject(
        content_type=content_type,
        canonical_key=canonical_key,
        display_code=display_code,
        info_hash=info_hash,
        source=source,
        source_item_id=source_item_id or info_hash or canonical_key,
        magnet=magnet or (f"magnet:?xt=urn:btih:{info_hash}" if info_hash else None),
        raw_title=display_code,
    )


def hex_hash(seed: int) -> str:
    """Deterministic 40-character info hash for tests."""

    return f"{seed:040x}"


class FakeDownloader:
    """In-memory downloader implementing the spec 17.3 protocol."""

    name = DownloaderName.QBITTORRENT

    def __init__(self) -> None:
        self.torrents: dict[str, DownloadSnapshot] = {}
        self.added: list[DownloadRequest] = []
        self.removed: list[tuple[str, bool]] = []
        self.add_should_fail = False
        self.remove_should_fail = False

    async def add(self, request: DownloadRequest) -> DownloadHandle:
        self.added.append(request)
        if self.add_should_fail:
            raise DownloaderRejectedError("fake downloader rejected the add")
        self.torrents[request.info_hash] = DownloadSnapshot(
            handle=DownloadHandle(self.name, request.info_hash, request.info_hash),
            state="downloading",
            progress=0.0,
            name=request.job_id,
            save_path=request.save_path,
        )
        return DownloadHandle(self.name, request.info_hash, request.info_hash)

    async def get(self, handle: DownloadHandle) -> DownloadSnapshot | None:
        return self.torrents.get(handle.info_hash)

    async def list_active(self) -> list[DownloadSnapshot]:
        return list(self.torrents.values())

    async def remove(self, handle: DownloadHandle, *, delete_files: bool = False) -> None:
        if delete_files:
            raise ValueError("delete_files=True is forbidden")
        self.removed.append((handle.info_hash, delete_files))
        if self.remove_should_fail:
            raise DownloaderProtocolError("fake remove failure")
        self.torrents.pop(handle.info_hash, None)

    async def close(self) -> None:
        return None

    # -- test helpers -------------------------------------------------------

    def complete(self, info_hash: str, content_path: Path) -> None:
        snapshot = self.torrents[info_hash]
        self.torrents[info_hash] = replace(
            snapshot,
            state="uploading",
            progress=1.0,
            content_path=str(content_path),
        )

    def fail_state(self, info_hash: str, state: str = "error") -> None:
        snapshot = self.torrents[info_hash]
        self.torrents[info_hash] = replace(snapshot, state=state, progress=1.0)


class FakeTelegramBot:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail = fail

    async def send_message(self, chat_id: int, text: str) -> None:
        if self.fail:
            raise RuntimeError("fake telegram is down")
        self.sent.append((chat_id, text))


async def create_job(
    db: object,
    *,
    status: object = None,
    info_hash: str | None = None,
    canonical_key: str = "fc2_ppv:1234567",
    category: str = "fc2",
    content_type: ContentType = ContentType.FC2,
    display_code: str | None = "FC2-PPV-1234567",
    raw_title: str | None = "FC2-PPV-1234567 test",
) -> tuple[str, str, str]:
    """Insert content + release + job directly, bypassing the gate.

    Used by tests that exercise the state machine, monitor and organizer in
    isolation.
    """

    from app.db.enums import DownloaderName, DownloadStatus
    from app.db.models import Content, DownloadJob, Release

    async with db.transaction() as session:  # type: ignore[attr-defined]
        content = Content(
            content_type=content_type,
            canonical_key=canonical_key,
            display_code=display_code,
        )
        session.add(content)
        await session.flush()
        release = Release(
            content_id=content.id,
            source="manual",
            source_item_id=info_hash or canonical_key,
            magnet=f"magnet:?xt=urn:btih:{info_hash}" if info_hash else None,
            info_hash=info_hash,
            raw_title=raw_title,
        )
        session.add(release)
        await session.flush()
        job = DownloadJob(
            release_id=release.id,
            content_id=content.id,
            downloader=DownloaderName.QBITTORRENT,
            status=status or DownloadStatus.ACCEPTED,
            category=category,
            idempotency_key=f"download:{release.id}",
            info_hash=info_hash,
        )
        session.add(job)
        await session.flush()
        return content.id, release.id, job.id
