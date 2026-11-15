"""Silent first-run backfill — spec 6.2 / 9.

A fresh install backfills the lookback window; those contents must never be pushed
as cards, otherwise the first round floods the chat with hundreds of messages. Only
contents discovered after the first successful round are pushed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.config import Settings
from app.db.enums import ContentType
from app.db.models import Content, Notification, Release
from app.db.session import Database
from app.schemas.release import NormalizedContent, ReleaseCandidate
from app.services.gate import ContentGate
from app.services.normalize import canonical_fc2_key, display_fc2_code
from app.services.push_queue import find_pending_push_candidates
from app.services.releases import RegistrationOutcome, RegistrationResult, ReleaseRegistrar
from app.services.subscription import SubscriptionPoller
from app.sources.nyaa import DiscoveryStats, NyaaCursor
from sqlalchemy import select

from tests.support import hex_hash, make_settings


def candidate(item_id: str, number: str) -> ReleaseCandidate:
    info_hash = hex_hash(int(number[-4:]))
    return ReleaseCandidate(
        source="nyaa",
        source_item_id=item_id,
        source_url=f"https://sukebei.nyaa.si/view/{item_id}",
        title=f"FC2-{number} title",
        info_hash=info_hash,
        magnet=f"magnet:?xt=urn:btih:{info_hash}",
        published_at=datetime.now(UTC),
        content_hint=NormalizedContent(
            content_type=ContentType.FC2,
            canonical_key=canonical_fc2_key(number),
            display_code=display_fc2_code(number),
        ),
    )


async def test_backfilled_content_is_flagged(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    result = await registrar.handle(candidate("1900001", "1111111"), backfill=True)

    assert result.outcome is RegistrationOutcome.RECORDED
    async with db.session() as session:
        content = await session.scalar(select(Content))
    assert content is not None and content.backfilled is True


async def test_normal_discovery_is_not_backfilled(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    await registrar.handle(candidate("1900002", "2222222"))

    async with db.session() as session:
        content = await session.scalar(select(Content))
    assert content is not None and content.backfilled is False


async def test_backfilled_content_never_enters_the_push_queue(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    await registrar.handle(candidate("1900003", "3333333"), backfill=True)
    await registrar.handle(candidate("1900004", "4444444"))

    async with db.session() as session:
        candidates = await find_pending_push_candidates(session)

    assert [item.canonical_key for item in candidates] == ["fc2_ppv:4444444"]


async def test_backfilled_content_stays_out_even_without_notification(db: Database) -> None:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    await registrar.handle(candidate("1900005", "5555555"), backfill=True)

    async with db.session() as session:
        assert await session.scalar(select(Notification)) is None
        assert await session.scalar(select(Release)) is not None
        candidates = await find_pending_push_candidates(session)

    assert candidates == []


class RecordingRegistrar:
    def __init__(self) -> None:
        self.backfill_flags: list[bool] = []

    async def handle(
        self, candidate: ReleaseCandidate, *, actor: str = "source", backfill: bool = False
    ) -> RegistrationResult:
        self.backfill_flags.append(backfill)
        return RegistrationResult(outcome=RegistrationOutcome.RECORDED)


class FakeSource:
    name = "nyaa"

    def __init__(self, rounds: list[list[ReleaseCandidate]]) -> None:
        self.rounds = list(rounds)
        self.cursors: list[dict[str, object] | None] = []
        self.last_cursor = NyaaCursor(etag='"v1"')
        self.last_stats = DiscoveryStats(rss_items=1, pages_fetched=1)

    async def discover(self, cursor: dict[str, object] | None) -> list[ReleaseCandidate]:
        self.cursors.append(cursor)
        current = self.rounds.pop(0) if self.rounds else []
        self.last_stats = DiscoveryStats(rss_items=len(current), pages_fetched=1)
        return current


def poller(
    db: Database, settings: Settings, source: FakeSource, registrar: RecordingRegistrar
) -> SubscriptionPoller:
    return SubscriptionPoller(
        db=db,
        source=source,
        registrar=registrar,
        settings=settings,  # type: ignore[arg-type]
    )


async def test_first_round_is_backfill_and_the_second_is_not(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    source = FakeSource([[candidate("1900006", "6666666")], [candidate("1900007", "7777777")]])
    registrar = RecordingRegistrar()

    await poller(db, settings, source, registrar).poll_once()
    await poller(db, settings, source, registrar).poll_once()

    assert registrar.backfill_flags == [True, False]


async def test_backfill_can_be_disabled_by_configuration(db: Database, tmp_path: Path) -> None:
    settings = make_settings(tmp_path, nyaa={"initial_backfill_silent": False})
    source = FakeSource([[candidate("1900008", "8888888")]])
    registrar = RecordingRegistrar()

    await poller(db, settings, source, registrar).poll_once()

    assert registrar.backfill_flags == [False]
