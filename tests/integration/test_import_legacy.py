"""Legacy importer and Transmission migration — one-off data adoption.

All fixtures are constructed here; no real legacy data is used.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.config import Settings
from app.db.enums import (
    AcceptedOrigin,
    ContentType,
    DownloaderName,
    DownloadStatus,
    NotificationStatus,
)
from app.db.models import (
    AcceptedEntry,
    BlacklistEntry,
    Content,
    DownloadJob,
    Notification,
    Release,
)
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.organizer.monitor import DownloadMonitor
from app.organizer.mover import Organizer
from app.organizer.paths import PathMapper
from app.organizer.stability import StabilityTracker
from app.services.normalize import Fc2CodeRule
from scripts.import_legacy import (
    ImportCounts,
    MigrationReport,
    import_legacy,
    load_json,
    load_jsonl,
    migrate_transmission,
    parse_accepted,
    parse_blacklist,
    parse_state,
    parse_transmission,
)
from sqlalchemy import delete, func, select

from tests.support import FakeDownloader, make_settings

RULE = Fc2CodeRule(5, 8)
HASH_DONE = "a" * 40
HASH_TR_1 = "b" * 40
HASH_TR_2 = "c" * 40


# ---------------------------------------------------------------------------
# fixtures (constructed)
# ---------------------------------------------------------------------------


def write_fixtures(tmp_path: Path) -> dict[str, Path]:
    state = {
        "seen": {
            "5007844": {
                "hash": HASH_DONE,
                "title": "Pushed Title",
                "pub_ts": 1794682888.0,
                "messages": [3570, 3571, 3572],
                "chat_id": 555164303,
            },
            "5007845": {"hash": "f" * 40, "title": "Seen Only", "messages": []},
            "not-a-number": {"title": "Bad", "messages": [1], "chat_id": 1},
        }
    }
    blacklist = {
        "entries": [
            {"number": "5007846", "title": "Blocked", "added_ts": 1794682888.0},
            {"number": "5007846", "title": "Duplicate"},
            {"number": "garbage"},
        ]
    }
    dead = [
        {"number": "5007847", "title": "Dead One"},
        {"number": "5007847", "title": "Dead One Again"},
    ]
    done = [
        {
            "number": "5007848",
            "title": "Done One",
            "info_hash": "d" * 40,
            "done_at": "2026-11-01T00:00:00Z",
        }
    ]
    claims = [{"number": "5007848"}, {"number": "5007849"}]
    pending = [
        {"number": "5007850", "phase": "pending"},
        {"number": "5007850", "phase": "ack"},
        {"number": "5007848"},  # already in done
    ]

    paths = {}
    for name, payload in (
        ("state", state),
        ("blacklist", blacklist),
        (
            "transmission",
            [
                {
                    "name": "FC2-PPV-5007851 Sample",
                    "hash": HASH_TR_1,
                    "code": "5007851",
                    "download_dir": "/9KG",
                    "size_bytes": 1000,
                    "status": "finished",
                },
                {"name": "no code here", "hash": "e" * 40, "status": "finished"},
                {
                    "name": "FC2-5007852 Missing Dir",
                    "hash": HASH_TR_2,
                    "code": "5007852",
                    "status": "finished",
                },
            ],
        ),
    ):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        paths[name] = path

    for name, records in (("dead", dead), ("done", done), ("claims", claims), ("pending", pending)):
        path = tmp_path / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
        paths[name] = path
    return paths


def datasets(paths: dict[str, Path]):
    return {
        "pushed": parse_state(load_json(paths["state"])),
        "blacklist": parse_blacklist(load_json(paths["blacklist"])),
        "done": parse_accepted(load_jsonl(paths["done"])),
        "claims": parse_accepted(load_jsonl(paths["claims"])),
        "pending": parse_accepted(load_jsonl(paths["pending"])),
    }


async def run_import(db: Database, paths: dict[str, Path], **kwargs):
    data = datasets(paths)
    counts = ImportCounts(pending_lines_read=len(load_jsonl(paths["pending"])))
    dead_records = [
        type(data["pushed"][0])(
            number=record["number"], title=record.get("title"), chat_id=None, message_ids=[]
        )
        for record in load_jsonl(paths["dead"])
    ]
    async with db.transaction() as session:
        await import_legacy(
            session,
            rule=RULE,
            pushed=data["pushed"],
            dead=dead_records,
            blacklist=data["blacklist"],
            done=data["done"],
            claims=data["claims"],
            pending=data["pending"],
            default_chat_id=111,
            counts=counts,
            **kwargs,
        )
    return counts


async def count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# ---------------------------------------------------------------------------
# importer
# ---------------------------------------------------------------------------


async def test_import_maps_every_dataset(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths)

    # 5007844 pushed, 5007846 blacklisted, 5007847 dead, 5007848/49/50 accepted
    assert counts.stat("torrentino_pushed").notifications == 1
    assert counts.stat("torrentino_blacklist").blacklist == 1
    assert counts.stat("torrentino_dead").notifications == 1
    assert counts.stat("torrentino_done").accepted == 1
    assert counts.stat("torrentino_done").releases == 1
    assert counts.stat("torrentino_claims").accepted == 1  # 5007848 already accepted
    # The pending file is a card-deletion queue, never imported.
    assert "torrentino_pending" not in counts.sources
    assert counts.pending_lines_read == 3
    assert counts.stat("torrentino_pushed").conflicts == 1  # not-a-number

    async with db.session() as session:
        notification = await session.scalar(
            select(Notification).where(Notification.bot_name == "legacy:torrentino")
        )
        accepted = list((await session.scalars(select(AcceptedEntry))).all())
        blacklist = await session.scalar(select(BlacklistEntry))
        release = await session.scalar(select(Release))
        dead = await session.scalar(
            select(Notification).where(Notification.status == NotificationStatus.LEGACY_DEAD)
        )

    assert notification is not None
    assert notification.status is NotificationStatus.LEGACY
    assert notification.poster_message_id == 3570
    assert notification.media_message_ids_json == [3571, 3572]
    assert notification.chat_id == 555164303
    assert blacklist is not None and blacklist.canonical_key == "fc2_ppv:5007846"
    assert release is not None and release.source == "legacy"
    assert release.info_hash == "d" * 40
    assert dead is not None and dead.content_id
    assert {entry.origin for entry in accepted} == {AcceptedOrigin.LEGACY_IMPORT}


async def test_pending_file_is_only_counted(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths)

    assert counts.pending_lines_read == 3
    assert "torrentino_pending" not in counts.sources
    async with db.session() as session:
        pending_content = await session.scalar(
            select(Content).where(Content.canonical_key == "fc2_ppv:5007850")
        )
    assert pending_content is None


def test_importer_docstring_documents_pending_as_never_imported() -> None:
    """Guard the documented rule: the pending file is a card-deletion queue."""

    from scripts import import_legacy as importer_module

    docstring = importer_module.__doc__ or ""
    pending_line = next(
        (line for line in docstring.splitlines() if "fc2_done.jsonl.pending" in line),
        "",
    )
    assert "never imported" in pending_line
    assert "not already in" not in docstring


async def test_import_is_idempotent(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    await run_import(db, paths)
    before = {
        "contents": await count(db, Content),
        "notifications": await count(db, Notification),
        "accepted": await count(db, AcceptedEntry),
        "blacklist": await count(db, BlacklistEntry),
        "releases": await count(db, Release),
    }

    counts = await run_import(db, paths)

    assert counts.totals()["created"] == 0
    assert counts.totals()["skipped"] > 0
    after = {
        "contents": await count(db, Content),
        "notifications": await count(db, Notification),
        "accepted": await count(db, AcceptedEntry),
        "blacklist": await count(db, BlacklistEntry),
        "releases": await count(db, Release),
    }
    assert before == after


async def test_import_does_not_modify_existing_content(db: Database, tmp_path: Path) -> None:
    async with db.transaction() as session:
        session.add(
            Content(
                content_type=ContentType.FC2,
                canonical_key="fc2_ppv:5007844",
                display_code="FC2-PPV-5007844",
                title_ja="KEEP ME",
            )
        )
    paths = write_fixtures(tmp_path)
    await run_import(db, paths)

    async with db.session() as session:
        content = await session.scalar(
            select(Content).where(Content.canonical_key == "fc2_ppv:5007844")
        )
    assert content is not None and content.title_ja == "KEEP ME"


async def test_blacklisted_numbers_are_not_pushed(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    # Make the blacklist and the pushed record the same number.
    state = json.loads(paths["state"].read_text())
    state["seen"]["5007846"] = {"messages": [1], "chat_id": 5, "title": "x"}
    paths["state"].write_text(json.dumps(state), encoding="utf-8")

    counts = await run_import(db, paths)

    reasons = [conflict["reason"] for conflict in counts.conflicts]
    assert "pushed_and_blacklisted" in reasons
    async with db.session() as session:
        pushed = await session.scalar(select(Notification).where(Notification.chat_id == 5))
    assert pushed is None


async def test_since_filters_by_number(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths, since=5007850)

    assert counts.stat("torrentino_pushed").notifications == 0
    assert counts.stat("torrentino_blacklist").blacklist == 0
    assert counts.stat("torrentino_done").accepted == 0
    assert counts.totals()["skipped"] > 0


# ---------------------------------------------------------------------------
# transmission migration
# ---------------------------------------------------------------------------


def migration_settings(tmp_path: Path, **overrides: object) -> Settings:
    return make_settings(
        tmp_path,
        storage={
            "path_mappings": [{"remote": "/9KG", "local": str(tmp_path / "9KG")}],
            "final_roots": {
                "fc2": str(tmp_path / "library" / "fc2"),
                "jav": str(tmp_path / "library" / "jav"),
                "cn": str(tmp_path / "library" / "cn"),
                "western": str(tmp_path / "library" / "western"),
                "review": str(tmp_path / "library" / "review"),
            },
        },
        **overrides,
    )


def _task(**overrides: object):
    from scripts.import_legacy import TransmissionTask

    base = {
        "name": "FC2-PPV-5007900 X",
        "info_hash": "9" * 40,
        "code": "5007900",
        "download_dir": "/9KG",
        "size_bytes": 100,
        "status": None,
        "qb_save_path": None,
        "category": None,
        "error": None,
        "error_string": None,
        "percent_done": None,
    }
    base.update(overrides)
    return TransmissionTask(**base)  # type: ignore[arg-type]


def test_effective_status_from_error_and_percent() -> None:
    from scripts.import_legacy import effective_status

    # Tracker refused the hash but the payload is complete: still finished.
    assert (
        effective_status(
            _task(error=1, error_string="info hash is not authorized", percent_done=1.0)
        )
        == "finished"
    )
    assert effective_status(_task(error=1, percent_done=0.5)) == "incomplete"
    # Corrupt pieces: only trusted at 1.0 (the operator force-rechecks first).
    assert effective_status(_task(error=3, percent_done=1.0)) == "finished"
    assert effective_status(_task(error=3, percent_done=0.98)) == "incomplete"
    assert effective_status(_task(percent_done=0.2)) == "incomplete"
    assert effective_status(_task(status="seeding")) == "finished"
    assert effective_status(_task()) == "unknown"


def test_only_hashes_filters_tasks() -> None:
    from scripts.import_legacy import filter_tasks_by_hashes

    tasks = [_task(info_hash="a" * 40), _task(info_hash="b" * 40)]
    assert len(filter_tasks_by_hashes(tasks, {"a" * 40})) == 1
    assert filter_tasks_by_hashes(tasks, set()) == tasks


async def test_finished_migration_warns_on_corrupt_pieces(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = [_task(error=3, error_string="Piece is corrupt", percent_done=1.0)]
    report = MigrationReport(mode="finished", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished", report=report
        )

    assert report.created == 1
    assert (
        report.warnings and report.warnings[0]["warning"] == "error_3_piece_corrupt_verify_recheck"
    )


async def test_finished_mode_skips_an_incomplete_task(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = [_task(error=3, percent_done=0.4)]
    report = MigrationReport(mode="finished", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished", report=report
        )

    assert report.created == 0
    assert report.conflicts[0]["reason"] == "status_mismatch"


async def test_finished_migration_creates_organize_jobs(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission(load_json(write_fixtures(tmp_path)["transmission"]))
    report = MigrationReport(mode="finished", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished", report=report
        )

    assert report.created == 1
    # The code-less task is now identified by its hash, so both skips are about the
    # missing download_dir (one of them in the generic group).
    assert report.skipped == 2
    assert [item["reason"] for item in report.conflicts] == [
        "missing_download_dir",
        "missing_download_dir",
    ]
    action = next(item for item in report.actions if item.action == "organize")
    assert action.category == "fc2"
    assert action.target == str(tmp_path / "library" / "fc2" / "FC2-PPV-5007851")

    async with db.session() as session:
        job = await session.scalar(select(DownloadJob))
        accepted = await session.scalar(select(AcceptedEntry))
        release = await session.scalar(select(Release))
    assert job is not None
    assert job.status is DownloadStatus.DOWNLOADED
    assert job.downloader is DownloaderName.TRANSMISSION
    assert job.downloader_task_id == HASH_TR_1
    assert job.remote_save_path == "/9KG"
    assert job.remote_content_path == "/9KG/FC2-PPV-5007851 Sample"
    assert accepted is not None and accepted.origin is AcceptedOrigin.LEGACY_TRANSMISSION
    assert release is not None and release.source == "transmission"


async def test_finished_migration_is_idempotent(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission(load_json(write_fixtures(tmp_path)["transmission"]))

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished"
        )
    second = MigrationReport(mode="finished", dry_run=False)
    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished", report=second
        )

    assert second.created == 0
    assert any(item["reason"] in {"job_exists", "accepted_exists"} for item in second.conflicts)
    assert await count(db, DownloadJob) == 1


async def test_incomplete_migration_takes_over_with_qbittorrent(
    db: Database, tmp_path: Path
) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission(
        [
            {
                "name": "FC2-PPV-5007860 Half",
                "hash": "f" * 40,
                "code": "5007860",
                "status": "downloading",
                "qb_save_path": "/downloads/jav/job-x",
            },
            {
                "name": "FC2-5007861 No Qb Path",
                "hash": "0" * 40,
                "code": "5007861",
                "status": "downloading",
            },
        ]
    )
    report = MigrationReport(mode="incomplete", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="incomplete", report=report
        )

    assert report.created == 1
    assert report.conflicts[0]["reason"] == "missing_qb_save_path"
    async with db.session() as session:
        job = await session.scalar(select(DownloadJob))
    assert job is not None
    assert job.status is DownloadStatus.DOWNLOADING
    assert job.downloader is DownloaderName.QBITTORRENT
    assert job.remote_save_path == "/downloads/jav/job-x"


async def test_migrated_finished_job_goes_through_the_real_organizer(
    db: Database, tmp_path: Path
) -> None:
    """The organizer moves the files, then the tr task is removed with deleteFiles=false."""

    settings = migration_settings(tmp_path)
    tasks = parse_transmission(load_json(write_fixtures(tmp_path)["transmission"]))
    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished"
        )

    source = tmp_path / "9KG" / "FC2-PPV-5007851 Sample"
    source.mkdir(parents=True)
    (source / "video.mkv").write_bytes(b"x" * 4096)

    class FakeTransmission(FakeDownloader):
        name = DownloaderName.TRANSMISSION

    downloader = FakeTransmission()
    registry = DownloaderRegistry({DownloaderName.TRANSMISSION: downloader})
    monitor = DownloadMonitor(
        db=db,
        registry=registry,
        organizer=Organizer(),
        settings=settings,
        stability=StabilityTracker(required_checks=2, interval_seconds=0),
        mapper=PathMapper(settings.storage.path_mappings),
    )

    await monitor.poll_once()  # DOWNLOADED -> ORGANIZING -> ORGANIZED
    await monitor.poll_once()  # TASK_REMOVED

    library = tmp_path / "library" / "fc2" / "FC2-PPV-5007851"
    assert (library / "video.mkv").exists()
    assert not source.exists()
    assert downloader.removed == [(HASH_TR_1, False)]
    async with db.session() as session:
        job = await session.scalar(select(DownloadJob))
    assert job is not None and job.status is DownloadStatus.TASK_REMOVED


async def test_report_lists_created_contents_for_rollback(db: Database, tmp_path: Path) -> None:
    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths)

    assert "fc2_ppv:5007844" in counts.created_content_keys
    assert "fc2_ppv:5007846" in counts.created_content_keys
    assert counts.as_dict()["created_content_keys"] == sorted(set(counts.created_content_keys))


async def test_rollback_keeps_referenced_contents(db: Database, tmp_path: Path) -> None:
    from scripts.import_legacy import delete_imported_contents

    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths)
    keys = list(counts.created_content_keys)

    # Remove the imported markers first, then the unreferenced contents.
    async with db.transaction() as session:
        await session.execute(delete(AcceptedEntry))
        await session.execute(delete(BlacklistEntry))
        await session.execute(delete(Notification))
        await session.execute(delete(Release))
    async with db.transaction() as session:
        deleted, kept = await delete_imported_contents(session, keys)

    assert deleted == len(keys)
    assert kept == 0
    assert await count(db, Content) == 0


async def test_rollback_keeps_a_content_that_is_still_referenced(
    db: Database, tmp_path: Path
) -> None:
    from scripts.import_legacy import delete_imported_contents

    paths = write_fixtures(tmp_path)
    counts = await run_import(db, paths)
    keys = list(counts.created_content_keys)

    # Drop only the notification markers: the two contents that had nothing else
    # become unreferenced, everything else must survive.
    async with db.transaction() as session:
        await session.execute(delete(Notification))
    async with db.transaction() as session:
        deleted, kept = await delete_imported_contents(session, keys)

    assert deleted == 2  # 5007844 (pushed) and 5007847 (dead)
    assert kept == len(keys) - 2
    async with db.session() as session:
        gone = await session.scalar(
            select(Content).where(Content.canonical_key == "fc2_ppv:5007844")
        )
        still_there = await session.scalar(
            select(Content).where(Content.canonical_key == "fc2_ppv:5007848")
        )
    assert gone is None
    assert still_there is not None


async def test_migration_report_groups_by_category(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission(load_json(write_fixtures(tmp_path)["transmission"]))
    report = MigrationReport(mode="finished", dry_run=True)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="finished", report=report
        )

    summary = report.as_dict(settings)
    assert summary["counts"]["planned"] == 1
    assert summary["counts"]["bytes_total"] == 1000
    assert summary["by_category"]["fc2"]["files"] == 1
    assert summary["by_category"]["fc2"]["bytes"] == 1000
    assert summary["by_category"]["fc2"]["target"] == str(tmp_path / "library" / "fc2")


# -- generic (non-FC2) tasks: identified by info hash, filed into _review -------

GENERIC_NAME = "(HD1080P)(美女遊記)(fc4306363.45qi7skt) 素人撮影"
GENERIC_HASH = "9" * 40


def generic_task(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "name": GENERIC_NAME,
        "hash": GENERIC_HASH,
        "status": "downloading",
        "qb_save_path": "/downloads/review/job-g",
        "size_bytes": 2048,
    }
    base.update(overrides)
    return base


async def test_generic_task_without_a_code_becomes_an_infohash_content(
    db: Database, tmp_path: Path
) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission([generic_task()])
    report = MigrationReport(mode="incomplete", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="incomplete", report=report
        )

    assert report.created == 1
    async with db.session() as session:
        content = await session.scalar(select(Content))
        job = await session.scalar(select(DownloadJob))
        accepted = await session.scalar(select(AcceptedEntry))
        release = await session.scalar(select(Release))

    assert content is not None
    assert content.canonical_key == f"infohash:{GENERIC_HASH}"
    assert content.content_type is ContentType.GENERIC
    assert content.display_code == "(美女遊記) 素人撮影"
    assert job is not None
    assert job.category == "review"
    assert job.status is DownloadStatus.DOWNLOADING
    assert job.downloader is DownloaderName.QBITTORRENT
    assert job.remote_save_path == "/downloads/review/job-g"
    assert accepted is not None
    assert accepted.canonical_key == f"infohash:{GENERIC_HASH}"
    assert accepted.origin is AcceptedOrigin.LEGACY_TRANSMISSION
    assert release is not None
    assert release.source_item_id == GENERIC_HASH
    assert release.info_hash == GENERIC_HASH


async def test_generic_task_is_reported_in_its_own_group(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission(
        [
            generic_task(),
            {
                "name": "FC2-PPV-5007862 Coded",
                "hash": "7" * 40,
                "code": "5007862",
                "status": "downloading",
                "qb_save_path": "/downloads/fc2/job-c",
            },
        ]
    )
    report = MigrationReport(mode="incomplete", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="incomplete", report=report
        )

    groups = report.by_group()
    assert groups["transmission_generic"]["planned"] == 1
    assert groups["transmission"]["planned"] == 1
    generic_action = next(
        item for item in report.actions if item.group == "transmission_generic"
    )
    assert generic_action.category == "review"
    assert "review" in str(generic_action.target)


async def test_generic_task_without_a_hash_is_skipped(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission([{"name": "no code and no hash", "status": "downloading"}])
    report = MigrationReport(mode="incomplete", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="incomplete", report=report
        )

    assert report.created == 0
    assert report.conflicts[0]["reason"] == "missing_hash"
    assert report.by_group()["transmission_generic"]["skipped"] == 1


async def test_generic_task_with_an_existing_accepted_hash_is_skipped(
    db: Database, tmp_path: Path
) -> None:
    """The gate's info-hash check must already know about this torrent."""

    settings = migration_settings(tmp_path)
    tasks = parse_transmission([generic_task()])

    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.GENERIC,
            canonical_key=f"infohash:{GENERIC_HASH}",
            display_code="already there",
        )
        session.add(content)
        await session.flush()
        session.add(
            AcceptedEntry(
                canonical_key=f"infohash:{GENERIC_HASH}",
                content_id=content.id,
                origin=AcceptedOrigin.LEGACY_IMPORT,
            )
        )
    report = MigrationReport(mode="incomplete", dry_run=False)

    async with db.transaction() as session:
        await migrate_transmission(
            session, tasks=tasks, rule=RULE, settings=settings, mode="incomplete", report=report
        )

    assert report.created == 0
    assert report.conflicts[0]["reason"] == "accepted_exists"
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 0


async def test_generic_migration_is_idempotent(db: Database, tmp_path: Path) -> None:
    settings = migration_settings(tmp_path)
    tasks = parse_transmission([generic_task()])

    for _ in range(2):
        report = MigrationReport(mode="incomplete", dry_run=False)
        async with db.transaction() as session:
            await migrate_transmission(
                session, tasks=tasks, rule=RULE, settings=settings,
                mode="incomplete", report=report,
            )

    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Content)) == 1
        assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 1
        assert await session.scalar(select(func.count()).select_from(AcceptedEntry)) == 1
        assert await session.scalar(select(func.count()).select_from(Release)) == 1


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (GENERIC_NAME, "(美女遊記) 素人撮影"),
        ("[4K] Some Title (2024) [x265].mkv", "Some Title (2024)"),
        ("(HD1080P)Only Tag", "Only Tag"),
        ("", "unknown"),
    ],
)
def test_clean_torrent_name_strips_noise(raw: str, expected: str) -> None:
    from scripts.import_legacy import clean_torrent_name

    assert clean_torrent_name(raw) == expected
