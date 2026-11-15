"""Manual magnet submission end-to-end — spec 10 / 22.3 (fake downloader)."""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.db.enums import AcceptedOrigin, DownloaderName, DownloadStatus, GateReason
from app.db.models import AcceptedEntry, Content, DownloadJob
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from app.services.manual_ingest import ManualIngestService
from sqlalchemy import func, select
from structlog.testing import capture_logs

from tests.support import FakeDownloader, hex_hash

HASH = hex_hash(201)
FC2_MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=FC2-PPV-1234567"
GENERIC_MAGNET = f"magnet:?xt=urn:btih:{hex_hash(202)}&dn=Some+Random+Movie"


def build_service(
    db: Database,
    settings: Settings,
    gate: ContentGate,
    downloader: FakeDownloader,
) -> tuple[ManualIngestService, JobSubmitter]:
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: downloader})
    submitter = JobSubmitter(db=db, registry=registry, settings=settings)
    service = ManualIngestService(db=db, gate=gate, submitter=submitter, settings=settings)
    return service, submitter


async def test_fc2_magnet_is_accepted_and_submitted(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)

    result = await service.ingest(text=FC2_MAGNET, user_id=111)

    assert result.accepted is True
    assert result.job_status is DownloadStatus.SUBMITTED
    assert result.category == "fc2"
    assert result.display_code == "FC2-PPV-1234567"
    assert len(fake_downloader.added) == 1
    request = fake_downloader.added[0]
    assert result.job_id is not None
    assert result.job_id in request.save_path
    assert "/fc2/" in request.save_path
    assert request.info_hash == HASH

    async with db.session() as session:
        job = await session.get(DownloadJob, result.job_id)
        assert job is not None
        assert job.status is DownloadStatus.SUBMITTED
        assert job.submitted_at is not None
        accepted = await session.scalar(select(func.count()).select_from(AcceptedEntry))
    assert accepted == 1


async def test_duplicate_magnet_does_not_add_a_second_task(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)

    first = await service.ingest(text=FC2_MAGNET, user_id=111)
    second = await service.ingest(text=FC2_MAGNET, user_id=111)

    assert first.accepted
    assert second.accepted is False
    assert second.reason is GateReason.ALREADY_ACCEPTED
    assert len(fake_downloader.added) == 1

    async with db.session() as session:
        jobs = await session.scalar(select(func.count()).select_from(DownloadJob))
    assert jobs == 1


async def test_blacklisted_code_is_refused(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)
    async with db.transaction() as session:
        from app.db.models import BlacklistEntry

        session.add(BlacklistEntry(canonical_key="fc2_ppv:1234567", reason="user rejected"))

    result = await service.ingest(text=FC2_MAGNET, user_id=111)

    assert result.accepted is False
    assert result.reason is GateReason.BLACKLISTED
    assert fake_downloader.added == []
    async with db.session() as session:
        contents = await session.scalar(select(func.count()).select_from(Content))
    assert contents == 0


async def test_ambiguous_category_needs_confirmation_and_writes_nothing(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)

    result = await service.ingest(text=GENERIC_MAGNET, user_id=111)

    assert result.needs_category is True
    assert result.magnet is not None
    assert fake_downloader.added == []
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(Content)) == 0
        assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 0


async def test_confirmed_category_is_accepted(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)

    result = await service.ingest(text=GENERIC_MAGNET, user_id=111, category="jav")

    assert result.accepted is True
    assert result.category == "jav"
    assert result.display_code is None
    assert len(fake_downloader.added) == 1
    assert "/jav/" in fake_downloader.added[0].save_path


async def test_review_category_falls_back_to_review_root(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)

    result = await service.ingest(text=GENERIC_MAGNET, user_id=111, category="review")

    assert result.accepted is True
    assert result.category == "review"
    assert "/review/" in fake_downloader.added[0].save_path


async def test_downloader_failure_keeps_accepted_entry(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.add_should_fail = True
    service, _ = build_service(db, settings, gate, fake_downloader)

    result = await service.ingest(text=FC2_MAGNET, user_id=111)

    assert result.accepted is True
    assert result.job_status is DownloadStatus.FAILED
    assert "/retry" in result.message

    async with db.session() as session:
        job = await session.get(DownloadJob, result.job_id)
        assert job is not None
        assert job.status is DownloadStatus.FAILED
        assert job.error_code == "downloader_rejected"
        accepted = await session.scalar(select(func.count()).select_from(AcceptedEntry))
        entry = await session.scalar(select(AcceptedEntry))
    assert accepted == 1
    assert entry is not None
    assert entry.origin is AcceptedOrigin.MANUAL

    # The content is not pushed again, and no second job is created.
    again = await service.ingest(text=FC2_MAGNET, user_id=111)
    assert again.reason is GateReason.ALREADY_ACCEPTED
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 1


async def test_retry_reuses_the_same_job(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    fake_downloader.add_should_fail = True
    service, submitter = build_service(db, settings, gate, fake_downloader)
    result = await service.ingest(text=FC2_MAGNET, user_id=111)
    assert result.job_status is DownloadStatus.FAILED

    fake_downloader.add_should_fail = False
    assert result.job_id is not None
    status = await submitter.retry(result.job_id)

    assert status is DownloadStatus.SUBMITTED
    assert len(fake_downloader.added) == 2  # failed attempt + retry, same job
    async with db.session() as session:
        assert await session.scalar(select(func.count()).select_from(DownloadJob)) == 1
        job = await session.get(DownloadJob, result.job_id)
        assert job is not None
        assert job.retry_count == 1


async def test_invalid_magnet_is_reported(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)
    result = await service.ingest(text="magnet:?dn=no-hash-here", user_id=111)
    assert result.accepted is False
    assert result.reason is GateReason.INVALID_CANDIDATE
    assert "磁链解析失败" in result.message


async def test_save_path_is_per_job(
    db: Database, settings: Settings, gate: ContentGate, fake_downloader: FakeDownloader
) -> None:
    service, _ = build_service(db, settings, gate, fake_downloader)
    await service.ingest(text=FC2_MAGNET, user_id=111)
    await service.ingest(
        text=f"magnet:?xt=urn:btih:{hex_hash(203)}&dn=FC2-PPV-7654321", user_id=111
    )

    assert len(fake_downloader.added) == 2
    paths = {request.save_path for request in fake_downloader.added}
    assert len(paths) == 2
    for path in paths:
        assert Path(path).is_absolute()


async def test_non_magnet_text_is_logged_with_a_redacted_preview() -> None:
    """Nothing may be silent: the fallback handler always leaves a log line."""

    from app.bots.manual import reject_non_magnet

    answers: list[str] = []

    class FakeUser:
        id = 111

    class FakeChat:
        id = 111

    class FakeMessage:
        from_user = FakeUser()
        chat = FakeChat()

        async def answer(self, text: str) -> None:
            answers.append(text)

    with capture_logs() as logs:
        await reject_non_magnet(FakeMessage(), "随便说点什么 " + "x" * 300)

    assert answers == []  # no near-miss hint for plain chatter
    record = next(entry for entry in logs if entry["event"] == "bot.manual.no_magnet")
    assert record["user_id"] == 111
    assert len(record["preview"]) == 200
    assert record["hint_like"] is False


async def test_non_magnet_text_with_a_hint_gets_an_answer() -> None:
    from app.bots.manual import reject_non_magnet

    answers: list[str] = []

    class FakeUser:
        id = 111

    class FakeChat:
        id = 111

    class FakeMessage:
        from_user = FakeUser()
        chat = FakeChat()

        async def answer(self, text: str) -> None:
            answers.append(text)

    with capture_logs() as logs:
        await reject_non_magnet(FakeMessage(), "看这个 magnet 但没贴全")

    assert answers and "没有找到 magnet" in answers[0]
    record = next(entry for entry in logs if entry["event"] == "bot.manual.no_magnet")
    assert record["hint_like"] is True
