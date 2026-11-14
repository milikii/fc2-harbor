"""Subscription polling loop — spec 6.2 / 21.

Each round: read the persisted cursor, ask the source for candidates, hand every
candidate to the release registrar (which evaluates the gate before writing), then
persist the new cursor. A failed round never stops the loop: the failure counter
is incremented and the next round runs after the interval plus jitter.

Nothing is pushed here — the card pipeline is M4. M2's job is to keep the
``contents``/``releases`` tables accurate and deduplicated.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from app.config import Settings
from app.db.models import SourceCursor, utcnow
from app.db.session import Database
from app.logging import get_logger
from app.schemas.release import ReleaseCandidate
from app.services.releases import RegistrationOutcome, ReleaseRegistrar
from app.sources.nyaa import DiscoveryStats, NyaaCursor

_log = get_logger("subscription")


class SubscriptionSource(Protocol):
    """What the poller needs from a source adapter (spec 17.1 plus cursor state)."""

    name: str
    last_cursor: Any
    last_stats: DiscoveryStats

    async def discover(self, cursor: dict[str, Any] | None) -> list[ReleaseCandidate]: ...


@dataclass(slots=True)
class PollReport:
    fetched: int = 0
    recorded: int = 0
    candidates: int = 0
    blacklisted: int = 0
    duplicates: int = 0
    filtered: int = 0
    pages: int = 0
    not_modified: bool = False
    consecutive_failures: int = 0
    error_code: str | None = None


class SubscriptionPoller:
    def __init__(
        self,
        *,
        db: Database,
        source: SubscriptionSource,
        registrar: ReleaseRegistrar,
        settings: Settings,
        rng: random.Random | None = None,
    ) -> None:
        self.db = db
        self.source = source
        self.registrar = registrar
        self.settings = settings
        self._rng = rng or random.Random()  # noqa: S311 - poll jitter, not crypto

    # -- one round ----------------------------------------------------------

    async def poll_once(self) -> PollReport:
        cursor_json = await self._load_cursor()
        report = PollReport()

        try:
            candidates = await self.source.discover(cursor_json)
        except Exception as exc:
            report.error_code = type(exc).__name__
            report.consecutive_failures = await self._record_failure()
            _log.error(
                "subscription.round_failed",
                source=self.source.name,
                error_code=report.error_code,
                consecutive_failures=report.consecutive_failures,
            )
            return report

        stats = self.source.last_stats
        report.pages = stats.pages_fetched
        report.not_modified = stats.not_modified
        report.filtered = len(stats.filtered)
        report.fetched = stats.rss_items

        for candidate in candidates:
            try:
                result = await self.registrar.handle(candidate, actor="subscription")
            except Exception as exc:
                _log.exception(
                    "subscription.candidate_failed",
                    source=candidate.source,
                    release_id=candidate.source_item_id,
                    error_code=type(exc).__name__,
                )
                continue
            if result.outcome is RegistrationOutcome.RECORDED:
                report.recorded += 1
            elif result.outcome is RegistrationOutcome.CANDIDATE:
                report.candidates += 1
            elif result.outcome is RegistrationOutcome.BLACKLISTED:
                report.blacklisted += 1
            else:
                report.duplicates += 1

        await self._save_cursor(self.source.last_cursor)

        _log.info(
            "subscription.round",
            source=self.source.name,
            fetched=report.fetched,
            recorded=report.recorded,
            candidates=report.candidates,
            blacklisted=report.blacklisted,
            duplicates=report.duplicates,
            filtered=report.filtered,
            pages=report.pages,
            not_modified=report.not_modified,
        )
        return report

    # -- cursor persistence -------------------------------------------------

    async def _load_cursor(self) -> dict[str, object] | None:
        async with self.db.session() as session:
            row = await session.scalar(
                select(SourceCursor).where(SourceCursor.source == self.source.name)
            )
            if row is None or not row.cursor_json:
                return None
            return dict(row.cursor_json)

    async def _save_cursor(self, cursor: NyaaCursor | None) -> None:
        async with self.db.transaction() as session:
            row = await session.scalar(
                select(SourceCursor).where(SourceCursor.source == self.source.name)
            )
            if row is None:
                row = SourceCursor(source=self.source.name)
                session.add(row)
            if cursor is not None:
                row.cursor_json = cursor.to_dict()
            row.last_success_at = utcnow()
            row.consecutive_failures = 0

    async def _record_failure(self) -> int:
        async with self.db.transaction() as session:
            row = await session.scalar(
                select(SourceCursor).where(SourceCursor.source == self.source.name)
            )
            if row is None:
                # Set the counter explicitly: the column default only applies at
                # INSERT time, so an unflushed row would have None here.
                row = SourceCursor(source=self.source.name, consecutive_failures=0)
                session.add(row)
            row.consecutive_failures += 1
            failures = row.consecutive_failures
        return failures

    # -- loop ---------------------------------------------------------------

    async def run(self, stop_event: asyncio.Event) -> None:
        interval = self.settings.nyaa.poll_interval_seconds
        jitter = self.settings.nyaa.jitter_seconds
        _log.info(
            "subscription.started",
            source=self.source.name,
            interval_seconds=interval,
            jitter_seconds=jitter,
        )
        while not stop_event.is_set():
            try:
                await self.poll_once()
            except Exception:
                _log.exception("subscription.round_crashed", source=self.source.name)

            delay = interval + self._rng.uniform(0, jitter)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except TimeoutError:
                continue
        _log.info("subscription.stopped", source=self.source.name)
