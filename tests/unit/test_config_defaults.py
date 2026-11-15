"""Defaults that must work without any env override.

The deployment bug this covers: the shipped default ``database_url`` used a sync
SQLite URL while the app only creates an async engine, so a fresh install crashed
with "loaded 'pysqlite' is not async" before anything else ran. Tests always passed
``sqlite+aiosqlite:///…`` themselves, so the default was never exercised.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import ConfigError, Settings
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from tests.support import make_config

DEFAULT_URL = "sqlite+aiosqlite:////app/data/app.db"


def defaults(tmp_path: Path) -> Settings:
    """Settings built without an ``app`` section, i.e. with the shipped defaults."""

    config = make_config(tmp_path)
    config.pop("app")
    return Settings.from_mapping(config, env={})


def test_default_database_url_uses_the_async_driver(tmp_path: Path) -> None:
    settings = defaults(tmp_path)
    assert settings.app.database_url == DEFAULT_URL
    assert settings.app.database_url.startswith("sqlite+aiosqlite://")


async def test_default_database_url_can_open_an_async_engine(tmp_path: Path) -> None:
    settings = defaults(tmp_path)

    # Exactly what startup does: this raised InvalidRequestError with the old value.
    engine = create_async_engine(settings.app.database_url)
    try:
        assert engine.url.drivername == "sqlite+aiosqlite"
        assert engine.url.database == "/app/data/app.db"
    finally:
        await engine.dispose()

    # And the driver really connects; the shipped path is /app/data (container only),
    # so the same URL is pointed at a temp file for the round trip.
    url = make_url(settings.app.database_url).set(database=str(tmp_path / "default.db"))
    live = create_async_engine(url)
    try:
        async with live.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar() == 1
    finally:
        await live.dispose()


def test_sync_database_url_is_rejected_at_config_load(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["app"]["database_url"] = "sqlite:////app/data/app.db"

    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "async driver" in str(excinfo.value)


def test_example_config_ships_the_async_url() -> None:
    example = (Path(__file__).resolve().parents[2] / "config" / "example.yaml").read_text(
        encoding="utf-8"
    )
    assert "database_url: sqlite+aiosqlite:////app/data/app.db" in example
    assert "database_url: sqlite:////app/data/app.db" not in example


def test_subscription_defaults(tmp_path: Path) -> None:
    settings = defaults(tmp_path)
    assert settings.subscription.polling_enabled is True
    assert settings.subscription.max_push_per_round == 10
    assert settings.nyaa.initial_backfill_silent is True


def test_push_batch_bounds(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["subscription"] = {"max_push_per_round": 0}
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})

    config["subscription"] = {"max_push_per_round": 5}
    assert Settings.from_mapping(config, env={}).subscription.max_push_per_round == 5


def test_polling_can_be_disabled(tmp_path: Path) -> None:
    config = make_config(tmp_path)
    config["subscription"] = {"polling_enabled": False}
    settings = Settings.from_mapping(config, env={})
    assert settings.subscription.polling_enabled is False
