"""Release registration for discovered sources — spec 6.4 / 11.

The tension between spec 11.1 step 4 ("source item already processed") and spec
6.4 ("a later uploader of the same FC2 number only adds a candidate download
source") is resolved here:

* gate **ALLOWED** → write content + release, no job and no accepted entry. The
  push pipeline (M4) turns this release into a card.
* gate **ALREADY_ACCEPTED** → the content is already accepted, so the new source
  item is still stored as a *candidate download source*: a ``releases`` row, no
  job, no accepted entry, no push.
* gate **BLACKLISTED** → nothing is stored; the rejection is audited. A
  blacklisted number must not come back through a new uploader.
* duplicate / info-hash-already-owned → nothing to do.

The gate is always evaluated **before** the release is written, so step 4 stays
meaningful (M1 contract).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.enums import GateReason
from app.db.models import Content, Release
from app.db.session import Database
from app.logging import get_logger
from app.schemas.release import ReleaseCandidate
from app.services.gate import ContentGate, GateSubject, subject_from_candidate
from app.services.state import record_event

_log = get_logger("releases")


class RegistrationOutcome(StrEnum):
    RECORDED = "recorded"
    CANDIDATE = "candidate"
    BLACKLISTED = "blacklisted"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class RegistrationResult:
    outcome: RegistrationOutcome
    content_id: str | None = None
    release_id: str | None = None
    reason: GateReason | None = None


class ReleaseRegistrar:
    def __init__(self, *, db: Database, gate: ContentGate) -> None:
        self.db = db
        self.gate = gate

    async def handle(
        self,
        candidate: ReleaseCandidate,
        *,
        actor: str = "source",
        backfill: bool = False,
    ) -> RegistrationResult:
        subject = subject_from_candidate(candidate)
        if subject is None:
            # No reliable content identity: filter, never guess (spec 6.3).
            return RegistrationResult(
                outcome=RegistrationOutcome.DUPLICATE, reason=GateReason.INVALID_CANDIDATE
            )

        try:
            async with self.db.transaction() as session:
                return await self._handle_in_transaction(
                    session, subject, candidate, actor, backfill=backfill
                )
        except IntegrityError:
            # A concurrent writer or the unique info-hash index won the race.
            _log.info(
                "release.duplicate",
                canonical_key=subject.canonical_key,
                source=candidate.source,
                release_id=candidate.source_item_id,
            )
            return RegistrationResult(
                outcome=RegistrationOutcome.DUPLICATE,
                reason=GateReason.SOURCE_ITEM_ALREADY_PROCESSED,
            )

    async def _handle_in_transaction(
        self,
        session: AsyncSession,
        subject: GateSubject,
        candidate: ReleaseCandidate,
        actor: str,
        *,
        backfill: bool = False,
    ) -> RegistrationResult:
        decision = await self.gate.evaluate(session, subject)

        if decision.reason is GateReason.ALLOWED:
            content = await self.gate.ensure_content(session, subject)
            if backfill and not content.backfilled:
                content.backfilled = True
            release = self._new_release(content.id, candidate)
            session.add(release)
            await session.flush()
            await record_event(
                session,
                "release.discovered",
                content_id=content.id,
                canonical_key=subject.canonical_key,
                release_id=release.id,
                actor=actor,
                reason="backfill" if backfill else candidate.source,
            )
            _log.info(
                "release.recorded",
                canonical_key=subject.canonical_key,
                content_id=content.id,
                release_id=release.id,
                source=candidate.source,
            )
            return RegistrationResult(
                outcome=RegistrationOutcome.RECORDED,
                content_id=content.id,
                release_id=release.id,
                reason=decision.reason,
            )

        if decision.reason is GateReason.ALREADY_ACCEPTED:
            accepted_content = await session.scalar(
                select(Content).where(Content.canonical_key == subject.canonical_key)
            )
            if accepted_content is None:
                # Accepted entry without content should be impossible; skip safely.
                return RegistrationResult(
                    outcome=RegistrationOutcome.DUPLICATE, reason=decision.reason
                )
            existing = await session.scalar(
                select(Release.id).where(
                    Release.source == candidate.source,
                    Release.source_item_id == candidate.source_item_id,
                )
            )
            if existing is not None:
                return RegistrationResult(
                    outcome=RegistrationOutcome.DUPLICATE,
                    content_id=accepted_content.id,
                    release_id=existing,
                    reason=decision.reason,
                )
            release = self._new_release(accepted_content.id, candidate)
            session.add(release)
            await session.flush()
            await record_event(
                session,
                "release.candidate_recorded",
                content_id=accepted_content.id,
                canonical_key=subject.canonical_key,
                release_id=release.id,
                actor=actor,
                reason="already_accepted",
            )
            _log.info(
                "release.candidate",
                canonical_key=subject.canonical_key,
                content_id=accepted_content.id,
                release_id=release.id,
                source=candidate.source,
            )
            return RegistrationResult(
                outcome=RegistrationOutcome.CANDIDATE,
                content_id=accepted_content.id,
                release_id=release.id,
                reason=decision.reason,
            )

        if decision.reason is GateReason.BLACKLISTED:
            await record_event(
                session,
                "gate.rejected",
                canonical_key=subject.canonical_key,
                actor=actor,
                reason=GateReason.BLACKLISTED.value,
            )
            _log.info(
                "release.blacklisted",
                canonical_key=subject.canonical_key,
                source=candidate.source,
            )
            return RegistrationResult(
                outcome=RegistrationOutcome.BLACKLISTED, reason=decision.reason
            )

        # Already-processed source item or an info hash that belongs to an
        # existing job: nothing to write.
        return RegistrationResult(
            outcome=RegistrationOutcome.DUPLICATE,
            content_id=decision.content_id,
            reason=decision.reason,
        )

    @staticmethod
    def _new_release(content_id: str, candidate: ReleaseCandidate) -> Release:
        return Release(
            content_id=content_id,
            source=candidate.source,
            source_item_id=candidate.source_item_id,
            source_url=candidate.source_url,
            magnet=candidate.magnet,
            info_hash=candidate.info_hash,
            raw_title=candidate.title,
            published_at=candidate.published_at,
            raw_payload_json=candidate.raw_payload or None,
        )
