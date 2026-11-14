"""Downloader registry — one place that maps categories to adapters.

Spec references: section 14 (qBittorrent is the default), section 15
(Transmission is optional and disabled by default), section 8.2 (downloader
traffic is always direct, never proxied).
"""

from __future__ import annotations

from collections.abc import Mapping

from app.config import ConfigError, Settings
from app.db.enums import DownloaderName
from app.downloaders.base import Downloader
from app.downloaders.qbittorrent import QBittorrentClient
from app.downloaders.transmission import TransmissionClient
from app.logging import get_logger

_log = get_logger("downloader.registry")


class DownloaderRegistry:
    """Holds the configured downloader instances for the process lifetime."""

    def __init__(self, downloaders: Mapping[DownloaderName, Downloader]) -> None:
        if not downloaders:
            raise ConfigError("no downloader configured")
        self._downloaders: dict[DownloaderName, Downloader] = dict(downloaders)

    @classmethod
    def from_settings(cls, settings: Settings) -> DownloaderRegistry:
        instances: dict[DownloaderName, Downloader] = {}
        if settings.downloaders.qbittorrent.enabled:
            config = settings.downloaders.qbittorrent
            assert config.url and config.username and config.password  # validated by config
            instances[DownloaderName.QBITTORRENT] = QBittorrentClient(
                base_url=config.url,
                username=config.username,
                password=config.password.get_secret_value(),
                timeout_seconds=config.timeout_seconds,
                verify_tls=config.verify_tls,
            )
        if settings.downloaders.transmission.enabled:
            transmission = settings.downloaders.transmission
            assert transmission.url  # validated by config
            instances[DownloaderName.TRANSMISSION] = TransmissionClient(
                base_url=transmission.url,
                username=transmission.username,
                password=(
                    transmission.password.get_secret_value()
                    if transmission.password is not None
                    else None
                ),
                timeout_seconds=transmission.timeout_seconds,
            )
        registry = cls(instances)
        _log.info("downloader.registry.ready", downloaders=sorted(name.value for name in instances))
        return registry

    def get(self, name: DownloaderName | str) -> Downloader:
        key = DownloaderName(name) if isinstance(name, str) else name
        try:
            return self._downloaders[key]
        except KeyError as exc:
            raise ConfigError(f"downloader {key.value!r} is not enabled") from exc

    def for_category(self, category: str, settings: Settings) -> Downloader:
        return self.get(settings.downloader_for_category(category))

    def names(self) -> list[str]:
        return sorted(name.value for name in self._downloaders)

    async def close_all(self) -> None:
        for downloader in self._downloaders.values():
            try:
                await downloader.close()
            except Exception:
                _log.warning("downloader.close_failed", downloader=downloader.name.value)
