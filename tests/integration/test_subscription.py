"""Subscription poller — spec 6.2 / 21 (cursor persistence, failure counter).

The source is faked: these tests cover the loop contract, not the parser.
"""

from __future__ import annotations

from typing import Any

from app.config import Settings
from app.db.enums import ContentType
from app.db.models import SourceCursor
from app.db.session import Database
from app.schemas.release import NormalizedContent, ReleaseCandidate
from app.services.gate import ContentGate
from app.services.normalize import canonical_fc2_key, display_fc2_code
from app.services.releases import ReleaseRegistrar
from app.services.subscription import SubscriptionPoller
from app.sources.nyaa import DiscoveryStats, NyaaCursor
from sqlalchemy import select

from tests.support import hex_hash


def candidate(item_id: str, number: str, info_hash: str) -> ReleaseCandidate:
    return ReleaseCandidate(
        source="nyaa",
        source_item_id=item_id,
        source_url=f"https://sukebei.nyaa.si/view/{item_id}",
        title=f"FC2-PPV-{number}",
        info_hash=info_hash,
        magnet=f"magnet:?xt=urn:btih:{info_hash}",
        content_hint=NormalizedContent(
            content_type=ContentType.FC2,
            canonical_key=canonical_fc2_key(number),
            display_code=display_fc2_code(number),
        ),
    )


class FakeSource:
    name = "nyaa"

    def __init__(self, rounds: list[Any]) -> None:
        self.rounds = list(rounds)
        self.cursors: list[dict[str, Any] | None] = []
        self.last_cursor: NyaaCursor | None = None
        self.last_stats = DiscoveryStats()

    async def discover(self, cursor: dict[str, Any] | None) -> list[ReleaseCandidate]:
        self.cursors.append(cursor)
        current = self.rounds.pop(0)
        if isinstance(current, Exception):
            raise current
        self.last_stats = DiscoveryStats(rss_items=len(current), pages_fetched=1)
        self.last_cursor = NyaaCursor(etag='"v1"', seen_item_ids=("x",))
        return list(current)


def build(db: Database, settings: Settings, source: FakeSource) -> SubscriptionPoller:
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())
    return SubscriptionPoller(db=db, source=source, registrar=registrar, settings=settings)


async def test_round_records_candidates_and_persists_cursor(
    db: Database, settings: Settings
) -> None:
    source = FakeSource(
        [
            [
                candidate("1900001", "1234567", hex_hash(601)),
                candidate("1900002", "7654321", hex_hash(602)),
            ],
            [],
        ]
    )
    poller = build(db, settings, source)

    first = await poller.poll_once()
    assert first.fetched == 2
    assert first.recorded == 2
    assert first.error_code is None

    async with db.session() as session:
        row = await session.scalar(select(SourceCursor).where(SourceCursor.source == "nyaa"))
    assert row is not None
    assert row.cursor_json is not None
    assert row.cursor_json["etag"] == '"v1"'
    assert row.last_success_at is not None
    assert row.consecutive_failures == 0

    # The next round receives the persisted cursor.
    second = await poller.poll_once()
    assert second.recorded == 0
    assert source.cursors[1] is not None
    assert source.cursors[1]["etag"] == '"v1"'


async def test_failed_round_increments_the_failure_counter(
    db: Database, settings: Settings
) -> None:
    source = FakeSource(
        [RuntimeError("site is down"), [candidate("1900003", "1111111", hex_hash(603))]]
    )
    poller = build(db, settings, source)

    failed = await poller.poll_once()
    assert failed.error_code == "RuntimeError"
    assert failed.consecutive_failures == 1

    async with db.session() as session:
        row = await session.scalar(select(SourceCursor).where(SourceCursor.source == "nyaa"))
    assert row is not None and row.consecutive_failures == 1

    ok = await poller.poll_once()
    assert ok.recorded == 1
    async with db.session() as session:
        row = await session.scalar(select(SourceCursor).where(SourceCursor.source == "nyaa"))
    assert row is not None and row.consecutive_failures == 0


async def test_one_bad_candidate_does_not_abort_the_round(
    db: Database, settings: Settings, monkeypatch: object
) -> None:
    source = FakeSource(
        [
            [
                candidate("1900004", "2222222", hex_hash(604)),
                candidate("1900005", "3333333", hex_hash(605)),
            ]
        ]
    )
    poller = build(db, settings, source)
    original = poller.registrar.handle
    calls = {"n": 0}

    async def flaky(candidate_obj: ReleaseCandidate, *, actor: str = "source") -> object:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("registrar exploded")
        return await original(candidate_obj, actor=actor)

    poller.registrar.handle = flaky  # type: ignore[method-assign]

    report = await poller.poll_once()

    assert report.recorded == 1
    assert report.error_code is None


async def test_already_accepted_candidates_are_counted_separately(
    db: Database, settings: Settings
) -> None:
    # The content was accepted through the manual bot / a button click.
    from app.db.enums import AcceptedOrigin
    from app.db.models import AcceptedEntry, Content

    canonical = canonical_fc2_key("4444444")
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=canonical,
            display_code=display_fc2_code("4444444"),
        )
        session.add(content)
        await session.flush()
        session.add(
            AcceptedEntry(
                canonical_key=canonical,
                content_id=content.id,
                origin=AcceptedOrigin.MANUAL,
            )
        )

    source = FakeSource([[candidate("1900007", "4444444", hex_hash(607))]])
    report = await build(db, settings, source).poll_once()

    assert report.candidates == 1
    assert report.recorded == 0
