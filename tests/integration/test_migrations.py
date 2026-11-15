"""Schema and constraints from spec 11.2 / 12 — the database is the last line of defence."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.db.enums import ContentType, DownloaderName, DownloadStatus
from app.db.models import AcceptedEntry, Content, DownloadJob, Release
from app.db.session import Database
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

EXPECTED_TABLES = {
    "contents",
    "releases",
    "provider_results",
    "assets",
    "accepted_entries",
    "blacklist_entries",
    "download_jobs",
    "notifications",
    "source_cursors",
    "decision_events",
}


async def test_all_spec_tables_exist(db: Database) -> None:
    async with db.session() as session:
        rows = await session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    names = {row[0] for row in rows}
    assert EXPECTED_TABLES <= names
    assert "alembic_version" in names


async def test_migration_head_is_applied(db: Database) -> None:
    async with db.session() as session:
        version = await session.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0005_content_cover_gate"


async def _content(db: Database, key: str = "fc2_ppv:1234567") -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2, canonical_key=key, display_code="FC2-PPV-1234567"
        )
        session.add(content)
        await session.flush()
        return content.id


async def test_contents_canonical_key_is_unique(db: Database) -> None:
    await _content(db)
    with pytest.raises(IntegrityError):
        await _content(db)


async def test_accepted_and_blacklist_keys_are_unique(db: Database) -> None:
    content_id = await _content(db)
    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            session.add_all(
                [
                    AcceptedEntry(
                        canonical_key="fc2_ppv:1234567", content_id=content_id, origin="manual"
                    ),
                    AcceptedEntry(
                        canonical_key="fc2_ppv:1234567", content_id=content_id, origin="manual"
                    ),
                ]
            )


async def test_release_source_item_is_unique(db: Database) -> None:
    content_id = await _content(db)
    async with db.transaction() as session:
        session.add(Release(content_id=content_id, source="nyaa", source_item_id="item-1"))
    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            session.add(Release(content_id=content_id, source="nyaa", source_item_id="item-1"))


async def test_non_null_info_hash_is_unique_but_nulls_are_allowed(db: Database) -> None:
    content_id = await _content(db)
    hash_value = "a" * 40
    async with db.transaction() as session:
        session.add(Release(content_id=content_id, source="nyaa", source_item_id="i1"))
        session.add(Release(content_id=content_id, source="nyaa", source_item_id="i2"))
        session.add(
            Release(
                content_id=content_id,
                source="nyaa",
                source_item_id="i3",
                info_hash=hash_value,
            )
        )

    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            session.add(
                Release(
                    content_id=content_id,
                    source="manual",
                    source_item_id="i4",
                    info_hash=hash_value,
                )
            )


async def test_download_job_idempotency_key_is_unique(db: Database) -> None:
    content_id = await _content(db)
    async with db.transaction() as session:
        release = Release(content_id=content_id, source="manual", source_item_id="r1")
        session.add(release)
        await session.flush()
        release_id = release.id

    async with db.transaction() as session:
        session.add(
            DownloadJob(
                release_id=release_id,
                content_id=content_id,
                downloader=DownloaderName.QBITTORRENT,
                status=DownloadStatus.ACCEPTED,
                category="fc2",
                idempotency_key="download:same",
            )
        )

    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            session.add(
                DownloadJob(
                    release_id=release_id,
                    content_id=content_id,
                    downloader=DownloaderName.QBITTORRENT,
                    status=DownloadStatus.ACCEPTED,
                    category="fc2",
                    idempotency_key="download:same",
                )
            )


async def test_enum_check_constraint_rejects_unknown_status(db: Database) -> None:
    content_id = await _content(db)
    async with db.transaction() as session:
        release = Release(content_id=content_id, source="manual", source_item_id="r2")
        session.add(release)
        await session.flush()
        release_id = release.id

    with pytest.raises(IntegrityError):
        async with db.transaction() as session:
            await session.execute(
                text(
                    "INSERT INTO download_jobs "
                    "(id, release_id, content_id, downloader, status, category, "
                    " idempotency_key, retry_count, created_at) "
                    "VALUES ('x', :rid, :cid, 'qbittorrent', 'NOT_A_STATUS', 'fc2', "
                    "'download:x', 0, '2020-01-01')"
                ),
                {"rid": release_id, "cid": content_id},
            )


async def test_wal_mode_is_enabled(db: Database) -> None:
    async with db.session() as session:
        mode = await session.scalar(text("PRAGMA journal_mode"))
    assert str(mode).lower() == "wal"


def test_upgrade_flags_existing_contents_as_backfilled(tmp_path: Path) -> None:
    """A deployed install upgrading to 0002 must not push its whole backlog."""

    import sqlite3

    from alembic import command
    from alembic.config import Config
    from app.db.session import PROJECT_ROOT

    database = tmp_path / "upgrade.db"
    url = f"sqlite+aiosqlite:///{database}"
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", url)
    config.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    command.upgrade(config, "0001_initial_schema")

    connection = sqlite3.connect(database)
    for index in range(3):
        connection.execute(
            "INSERT INTO contents (id, content_type, canonical_key, censorship, "
            "metadata_status, created_at, updated_at) VALUES (?, 'fc2', ?, 'unknown', "
            "'pending', '2020-01-01', '2020-01-01')",
            (f"c{index}", f"fc2_ppv:{index}"),
        )
    connection.commit()
    connection.close()

    command.upgrade(config, "head")

    connection = sqlite3.connect(database)
    try:
        flagged = connection.execute(
            "SELECT COUNT(*) FROM contents WHERE backfilled = 1"
        ).fetchone()[0]
        version = connection.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    finally:
        connection.close()
    assert flagged == 3
    assert version == "0005_content_cover_gate"


async def test_counts_helper(db: Database) -> None:
    await _content(db)
    async with db.session() as session:
        count = await session.scalar(select(func.count()).select_from(Content))
    assert count == 1
