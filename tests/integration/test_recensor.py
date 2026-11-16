"""Recompute censorship tags for content already in the database.

No HTTP: the script only reads what is stored (title, description, release title).
Fixtures are constructed here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.db.enums import Censorship, ContentType
from app.db.models import Content, ProviderResult, Release
from app.db.session import Database
from scripts.recensor import (
    RecensorReport,
    _DryRunRollbackError,
    recensor,
)
from sqlalchemy import select


async def seed(
    db: Database,
    key: str = "fc2_ppv:1234567",
    *,
    title: str | None = None,
    description: str | None = None,
    release_title: str | None = None,
    censorship: Censorship = Censorship.UNKNOWN,
    provider_raw: dict[str, object] | None = None,
) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=key,
            display_code="FC2-PPV-1234567",
            title_ja=title,
            description_ja=description,
            censorship=censorship,
        )
        session.add(content)
        await session.flush()
        if release_title is not None:
            session.add(
                Release(
                    content_id=content.id,
                    source="nyaa",
                    source_item_id="1900001",
                    raw_title=release_title,
                    published_at=datetime.now(UTC),
                )
            )
        if provider_raw is not None:
            session.add(
                ProviderResult(
                    content_id=content.id,
                    provider="Fc2",
                    success=True,
                    result_json={"title_ja": title, "raw": provider_raw},
                    fetched_at=datetime.now(UTC),
                )
            )
        return content.id


async def tag_of(db: Database, content_id: str) -> Censorship:
    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        return content.censorship


async def test_unknown_title_becomes_censored(db: Database) -> None:
    content_id = await seed(db, title="【有】素人作品")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.CENSORED
    assert report.changes[0]["old"] == "unknown"
    assert report.changes[0]["new"] == "censored"
    assert "title" in report.changes[0]["fields"]


async def test_unknown_release_title_becomes_uncensored(db: Database) -> None:
    content_id = await seed(
        db, title="素人作品", release_title="FC2-PPV-1234567 【無】素人作品"
    )
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.UNCENSORED
    assert report.changes[0]["tokens"] == ["【無】"]
    assert "release_title" in report.changes[0]["fields"]


async def test_existing_tag_is_kept_without_force(db: Database) -> None:
    content_id = await seed(
        db, title="【無】作品", censorship=Censorship.CENSORED
    )
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 0
    assert report.kept == 1
    assert await tag_of(db, content_id) is Censorship.CENSORED


async def test_force_overwrites_an_existing_tag(db: Database) -> None:
    content_id = await seed(db, title="【無】作品", censorship=Censorship.CENSORED)
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=True, report=report)

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.UNCENSORED


async def test_second_run_changes_nothing(db: Database) -> None:
    await seed(db, title="【有】作品")
    first = RecensorReport(dry_run=False)
    async with db.transaction() as session:
        await recensor(session, force=False, report=first)
    assert first.changed == 1

    second = RecensorReport(dry_run=False)
    async with db.transaction() as session:
        await recensor(session, force=False, report=second)
    assert second.changed == 0
    assert second.unchanged == 1


async def test_evidence_is_merged_into_the_existing_raw(db: Database) -> None:
    content_id = await seed(
        db,
        title="【薄】作品",
        provider_raw={"provider_url": "https://adult.contents.fc2.com/article/1/",
                      "matched": True, "description_source": "meta"},
    )
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.raw_updated == 1
    async with db.session() as session:
        row = await session.scalar(
            select(ProviderResult).where(ProviderResult.content_id == content_id)
        )
    assert row is not None and row.result_json is not None
    raw = row.result_json["raw"]
    # Nothing else was lost.
    assert raw["provider_url"] == "https://adult.contents.fc2.com/article/1/"
    assert raw["matched"] is True
    assert raw["description_source"] == "meta"
    assert raw["censorship_evidence"]["matches"][0]["token"] == "【薄】"


async def test_no_evidence_leaves_unknown_and_is_counted(db: Database) -> None:
    content_id = await seed(db, title="素人作品", description="説明文です。")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 0
    assert report.no_evidence == 1
    assert await tag_of(db, content_id) is Censorship.UNKNOWN


async def test_dry_run_reports_changes_but_writes_nothing(db: Database) -> None:
    content_id = await seed(db, title="【有】作品")
    report = RecensorReport(dry_run=True)

    try:
        async with db.transaction() as session:
            await recensor(session, force=False, report=report)
            raise _DryRunRollbackError
    except _DryRunRollbackError:
        pass

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.UNKNOWN


async def test_report_distribution_uses_the_chinese_labels(db: Database) -> None:
    await seed(db, "fc2_ppv:1", title="【有】作品")
    await seed(db, "fc2_ppv:2", title="【無】作品")
    await seed(db, "fc2_ppv:3", title="素人作品")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    distribution = report.as_dict()["distribution"]
    assert distribution["before"] == {"有码": 0, "无码": 0, "薄码": 0, "未知": 3}
    assert distribution["after"] == {"有码": 1, "无码": 1, "薄码": 0, "未知": 1}


def test_report_file_is_json_serialisable(tmp_path: Path) -> None:
    report = RecensorReport(dry_run=True)
    report.changes.append({"canonical_key": "fc2_ppv:1", "old": "unknown", "new": "censored"})
    payload = json.dumps(report.as_dict(), ensure_ascii=False)
    assert "fc2_ppv:1" in payload
    assert json.loads(payload)["counts"]["changed"] == 0


@pytest.mark.parametrize("force", [False, True])
async def test_generic_content_without_any_wording_is_untouched(
    db: Database, force: bool
) -> None:
    content_id = await seed(db, "infohash:" + "a" * 40, title="Some Torrent")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=force, report=report)

    assert report.changed == 0
    assert await tag_of(db, content_id) is Censorship.UNKNOWN


async def test_run_creates_the_schema_and_applies_changes(tmp_path: Path) -> None:
    """The CLI path: migrate the database, run inside one transaction, commit."""

    import argparse

    import yaml
    from app.config import Settings
    from scripts.recensor import run

    from tests.support import make_config

    config = make_config(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    settings = Settings.from_mapping(config, env={})

    database = Database(settings.app.database_url)
    await migrate_and_seed(database, settings)
    await database.dispose()

    report = await run(
        argparse.Namespace(config=str(config_path), dry_run=False, force=False, report="x.json")
    )

    assert report.changed == 1
    db = Database(settings.app.database_url)
    try:
        async with db.session() as session:
            content = await session.scalar(select(Content))
    finally:
        await db.dispose()
    assert content is not None
    assert content.censorship is Censorship.CENSORED


async def migrate_and_seed(database: Database, settings: object) -> None:
    from app.db.session import migrate_async

    await migrate_async(settings.app.database_url)  # type: ignore[attr-defined]
    async with database.transaction() as session:
        session.add(
            Content(
                content_type=ContentType.FC2,
                canonical_key="fc2_ppv:5000001",
                display_code="FC2-PPV-5000001",
                title_ja="【有】素人作品",
                censorship=Censorship.UNKNOWN,
            )
        )


async def test_updated_at_is_not_touched(db: Database) -> None:
    """``updated_at`` means "when the metadata was fetched", not "when we rescored"."""

    content_id = await seed(db, title="【有】作品")
    stamp = datetime(2020, 11, 1, 12, 0, tzinfo=UTC)
    async with db.transaction() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        content.updated_at = stamp
    async with db.session() as session:
        before = (await session.get(Content, content_id)).updated_at  # type: ignore[union-attr]

    report = RecensorReport(dry_run=False)
    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    async with db.session() as session:
        after = await session.get(Content, content_id)
    assert after is not None
    assert report.changed == 1
    assert after.censorship is Censorship.CENSORED
    assert after.updated_at == before


async def test_report_explains_the_updated_at_history() -> None:
    report = RecensorReport(dry_run=True)
    notes = report.as_dict()["notes"]
    assert isinstance(notes, list) and notes
    assert any("updated_at" in str(note) for note in notes)


# -- official tags decide, and a fetched page defaults to censored -------------


async def seed_with_fc2_result(
    db: Database,
    *,
    tags: list[str] | None = None,
    success: bool = True,
    title: str | None = None,
) -> str:
    content_id = await seed(db, "fc2_ppv:1", title=title)
    async with db.transaction() as session:
        session.add(
            ProviderResult(
                content_id=content_id,
                provider="Fc2",
                success=success,
                result_json={"raw": {"tags": tags or []}},
                fetched_at=datetime.now(UTC),
            )
        )
    return content_id


async def test_official_tags_decide_the_tag(db: Database) -> None:
    content_id = await seed_with_fc2_result(db, tags=["無修正", "顔射"])
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.UNCENSORED


async def test_a_fetched_page_without_censor_tags_defaults_to_censored(
    db: Database,
) -> None:
    content_id = await seed_with_fc2_result(db, tags=["顔射"])
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 1
    assert await tag_of(db, content_id) is Censorship.CENSORED


async def test_without_a_successful_fetch_only_titles_decide(db: Database) -> None:
    with_title = await seed(db, "fc2_ppv:1", title="【無】作品")
    no_evidence = await seed(db, "fc2_ppv:2", title="作品")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert await tag_of(db, with_title) is Censorship.UNCENSORED
    # Never guessed: no page, no title evidence.
    assert await tag_of(db, no_evidence) is Censorship.UNKNOWN


async def test_failed_fc2_result_does_not_trigger_the_default(db: Database) -> None:
    content_id = await seed_with_fc2_result(db, success=False, title="作品")
    report = RecensorReport(dry_run=False)

    async with db.transaction() as session:
        await recensor(session, force=False, report=report)

    assert report.changed == 0
    assert await tag_of(db, content_id) is Censorship.UNKNOWN
