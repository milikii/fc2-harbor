"""pytest fixtures: a migrated SQLite database per test and shared settings.

The schema is created **once per session** by running the real Alembic
migration, then the resulting file is copied for every test. That keeps the
tests fast while still exercising the migration that ships in the container.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from app.config import Settings
from app.db.session import Database, run_migrations
from app.services.gate import ContentGate

from tests.support import FakeDownloader, make_settings


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-live",
        action="store_true",
        default=False,
        help="run live network smoke tests (never in CI)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip ``live`` tests unless --run-live was passed (spec 22.2)."""

    if config.getoption("--run-live"):
        return
    skip_live = pytest.mark.skip(reason="live smoke test: pass --run-live to enable")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture(scope="session")
def migrated_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    directory = tmp_path_factory.mktemp("schema")
    template = directory / "template.db"
    run_migrations(f"sqlite+aiosqlite:///{template}")
    assert template.exists()
    # The migration engine runs in WAL mode; checkpoint so the copied file
    # contains everything (including the alembic_version row).
    with sqlite3.connect(template) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return template


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return make_settings(tmp_path)


@pytest.fixture
async def db(tmp_path: Path, migrated_template: Path) -> AsyncIterator[Database]:
    target = tmp_path / "app.db"
    shutil.copyfile(migrated_template, target)
    database = Database(f"sqlite+aiosqlite:///{target}")
    try:
        yield database
    finally:
        await database.dispose()


@pytest.fixture
def gate() -> ContentGate:
    return ContentGate()


@pytest.fixture
def fake_downloader() -> FakeDownloader:
    return FakeDownloader()


@pytest.fixture
def library_dirs(tmp_path: Path) -> Iterator[dict[str, Path]]:
    roots = {
        category: tmp_path / "library" / category
        for category in ("fc2", "jav", "cn", "western", "review")
    }
    for root in roots.values():
        root.mkdir(parents=True, exist_ok=True)
    yield roots
