#!/usr/bin/env python3
"""One-off importer: legacy fc2bot / torrentino / Transmission state → fc2-harbor.

Inputs (NAS layout by default, every path overridable):

===============================  =============================================
``fc2_state.json``               ``seen`` map. Records with a non-empty
                                 ``messages`` list were pushed → a legacy
                                 ``notifications`` row (status ``legacy``) so the
                                 card is never sent again
``fc2_dead.jsonl``               records seen but never delivered (download
                                 failed) → ``notifications`` with status
                                 ``legacy:dead``. **Not** blacklisted: the user
                                 never rejected them
``fc2_blacklist.json``           ``entries`` → ``blacklist_entries``
``fc2_done.jsonl``               finished downloads → ``accepted_entries``
                                 (origin ``legacy_import``) + a ``releases`` row
                                 when an info hash is known
``fc2_claims.jsonl``             dedupe claims → ``accepted_entries``
``fc2_done.jsonl.pending``       **never imported.** This is the legacy
                                 "delete this card" queue (``DoneDeletionQueue``:
                                 the download bot writes it, the FC2 bot deletes
                                 its own cards and writes an ack), not a download
                                 record. Importing it would wrongly block content;
                                 only its line count is reported
``--transmission-export X.json``  existing Transmission (tr-bt) tasks. Registered
                                 as accepted + release only — **no download job**,
                                 because those tasks are still seeding elsewhere
                                 and our monitor must not touch them
===============================  =============================================

Rules:

* idempotent: every write is "insert if absent", never delete-then-insert, so the
  script can be re-run safely;
* ``--dry-run`` runs the same code path inside a transaction and rolls it back, so
  the counts are real;
* existing rows are never modified; a contradictory legacy record is skipped and
  reported as a conflict;
* numbers are normalised with the configured :class:`Fc2CodeRule`;
* rollback markers: ``notifications.bot_name='legacy:torrentino'``,
  ``accepted_entries.origin LIKE 'legacy%'``, ``releases.source IN ('legacy',
  'transmission')``, ``blacklist_entries.reason='legacy_import'``.

Usage (inside the container):

    python scripts/import_legacy.py --dry-run
    python scripts/import_legacy.py --since 5007844
    python scripts/import_legacy.py --transmission-export /tmp/tr-export.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import CATEGORIES, Settings
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
    utcnow,
)
from app.db.session import Database, migrate_async
from app.organizer.paths import build_directory
from app.services.normalize import Fc2CodeRule, NormalizedContent
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

LEGACY_BOT = "legacy:torrentino"
LEGACY_ACTOR = "legacy:torrentino"
LEGACY_ORIGIN = AcceptedOrigin.LEGACY_IMPORT
TRANSMISSION_ORIGIN = AcceptedOrigin.LEGACY_TRANSMISSION
LEGACY_SOURCE = "legacy"
TRANSMISSION_SOURCE = "transmission"
LEGACY_REASON = "legacy_import"

SOURCE_PUSHED = "torrentino_pushed"
SOURCE_DEAD = "torrentino_dead"
SOURCE_DONE = "torrentino_done"
SOURCE_CLAIMS = "torrentino_claims"
SOURCE_PENDING = "torrentino_pending"
SOURCE_BLACKLIST = "torrentino_blacklist"
SOURCE_TRANSMISSION = "transmission"

DEFAULT_BASE = Path("/home/docker")
DEFAULTS = {
    "state": DEFAULT_BASE / "torrentino-fc2" / "config" / "fc2_state.json",
    "blacklist": DEFAULT_BASE / "torrentino-fc2" / "config" / "fc2_blacklist.json",
    "dead": DEFAULT_BASE / "fc2-shared" / "fc2_dead.jsonl",
    "done": DEFAULT_BASE / "fc2-shared" / "fc2_done.jsonl",
    "claims": DEFAULT_BASE / "fc2-shared" / "fc2_claims.jsonl",
    "pending": DEFAULT_BASE / "fc2-shared" / "fc2_done.jsonl.pending",
}


class LegacyImportError(RuntimeError):
    """Fatal import problem (missing file, unreadable JSON)."""


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SourceStats:
    name: str
    contents: int = 0
    notifications: int = 0
    accepted: int = 0
    releases: int = 0
    blacklist: int = 0
    skipped: int = 0
    conflicts: int = 0

    @property
    def created(self) -> int:
        return self.contents + self.notifications + self.accepted + self.releases + self.blacklist

    def as_dict(self) -> dict[str, int]:
        return {
            "created": self.created,
            "contents": self.contents,
            "notifications": self.notifications,
            "accepted": self.accepted,
            "releases": self.releases,
            "blacklist": self.blacklist,
            "skipped": self.skipped,
            "conflicts": self.conflicts,
        }


@dataclass(slots=True)
class ImportCounts:
    sources: dict[str, SourceStats] = field(default_factory=dict)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    pending_lines_read: int = 0
    #: canonical keys of the contents this run created, for a precise rollback.
    created_content_keys: list[str] = field(default_factory=list)

    def stat(self, name: str) -> SourceStats:
        return self.sources.setdefault(name, SourceStats(name=name))

    def add_conflict(self, number: str, source: str, reason: str) -> None:
        self.stat(source).conflicts += 1
        self.conflicts.append({"number": number, "source": source, "reason": reason})

    def totals(self) -> dict[str, int]:
        totals = {"created": 0, "skipped": 0, "conflicts": 0}
        for stats in self.sources.values():
            totals["created"] += stats.created
            totals["skipped"] += stats.skipped
            totals["conflicts"] += stats.conflicts
        return totals

    def as_dict(self) -> dict[str, Any]:
        return {
            **self.totals(),
            "pending_lines_read": self.pending_lines_read,
            "created_content_keys": sorted(set(self.created_content_keys)),
            "by_source": {name: stats.as_dict() for name, stats in self.sources.items()},
        }


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


def normalize_number(raw: object, rule: Fc2CodeRule) -> NormalizedContent | None:
    """Normalise a legacy number with the configured rule (no private regex)."""

    text = str(raw).strip()
    if not text:
        return None
    candidate = text if text.upper().startswith("FC2") else f"FC2-PPV-{text}"
    return rule.normalize(candidate)


def normalize_from_name(name: object, rule: Fc2CodeRule) -> NormalizedContent | None:
    return rule.normalize(str(name)) if name else None


def parse_timestamp(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=UTC)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def load_json(path: Path) -> Any:
    if not path.exists():
        raise LegacyImportError(f"file not found: {path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LegacyImportError(f"cannot read {path}: {exc}") from exc


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            value = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise LegacyImportError(f"{path}:{line_number}: invalid JSON ({exc})") from exc
        if isinstance(value, dict):
            records.append(value)
    return records


@dataclass(frozen=True, slots=True)
class LegacyPushed:
    number: str
    title: str | None
    chat_id: int | None
    message_ids: list[int]


@dataclass(frozen=True, slots=True)
class LegacyAccepted:
    number: str
    title: str | None
    info_hash: str | None
    done_at: datetime | None


@dataclass(frozen=True, slots=True)
class LegacyBlacklist:
    number: str
    title: str | None
    added_at: datetime | None
    note: str | None


@dataclass(frozen=True, slots=True)
class TransmissionTask:
    name: str | None
    info_hash: str | None
    code: str | None
    download_dir: str | None
    size_bytes: int | None = None
    status: str | None = None
    qb_save_path: str | None = None
    category: str | None = None
    error: int | None = None
    error_string: str | None = None
    percent_done: float | None = None


def parse_state(payload: Any) -> list[LegacyPushed]:
    seen = payload.get("seen", payload) if isinstance(payload, dict) else {}
    if not isinstance(seen, dict):
        raise LegacyImportError("fc2_state.json has no 'seen' mapping")
    pushed: list[LegacyPushed] = []
    for number, record in seen.items():
        if not isinstance(record, dict):
            continue
        messages = [int(value) for value in record.get("messages") or [] if value is not None]
        if not messages:
            continue
        chat_id = record.get("chat_id")
        pushed.append(
            LegacyPushed(
                number=str(number),
                title=record.get("title"),
                chat_id=int(chat_id) if chat_id is not None else None,
                message_ids=messages,
            )
        )
    return pushed


def parse_blacklist(payload: Any) -> list[LegacyBlacklist]:
    entries = payload.get("entries", payload) if isinstance(payload, dict) else payload
    if isinstance(entries, dict):
        entries = [
            {"number": key, **(value if isinstance(value, dict) else {})}
            for key, value in entries.items()
        ]
    if not isinstance(entries, list):
        raise LegacyImportError("fc2_blacklist.json has no 'entries' list")
    parsed: list[LegacyBlacklist] = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("number") is None:
            continue
        parsed.append(
            LegacyBlacklist(
                number=str(entry["number"]),
                title=entry.get("title"),
                added_at=parse_timestamp(entry.get("added_ts")),
                note=entry.get("note"),
            )
        )
    return parsed


def parse_accepted(records: list[dict[str, Any]]) -> list[LegacyAccepted]:
    parsed: list[LegacyAccepted] = []
    for record in records:
        number = record.get("number") or record.get("code")
        if number is None:
            continue
        info_hash = record.get("info_hash") or record.get("hash")
        parsed.append(
            LegacyAccepted(
                number=str(number),
                title=record.get("title"),
                info_hash=str(info_hash).lower() if info_hash else None,
                done_at=parse_timestamp(record.get("done_at") or record.get("ts")),
            )
        )
    return parsed


def _parse_size(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _parse_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def _parse_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def parse_transmission(payload: Any) -> list[TransmissionTask]:
    """Parse the operator's export.

    Field aliases are accepted because the export is produced by hand: ``hash`` /
    ``info_hash``, ``code`` / ``number``, ``download_dir`` / ``save_path``,
    ``size_bytes`` / ``total_size``, ``qb_save_path`` / ``savepath``.
    """

    if not isinstance(payload, list):
        raise LegacyImportError("transmission export must be a JSON list")
    parsed: list[TransmissionTask] = []
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        info_hash = entry.get("hash") or entry.get("info_hash")
        code = entry.get("code", entry.get("number"))
        size = entry.get("size_bytes", entry.get("total_size", entry.get("size")))
        parsed.append(
            TransmissionTask(
                name=entry.get("name"),
                info_hash=str(info_hash).lower() if info_hash else None,
                code=str(code) if code is not None else None,
                download_dir=entry.get("download_dir") or entry.get("save_path"),
                size_bytes=int(size)
                if isinstance(size, (int, float, str)) and str(size).isdigit()
                else None,
                status=(str(entry.get("status")).lower() if entry.get("status") else None),
                qb_save_path=entry.get("qb_save_path") or entry.get("savepath"),
                category=entry.get("category"),
                error=_parse_int(entry.get("error")),
                error_string=entry.get("error_string"),
                percent_done=_parse_float(entry.get("percent_done", entry.get("percentDone"))),
            )
        )
    return parsed


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


class _DryRunRollbackError(Exception):
    pass


class _Importer:
    def __init__(
        self,
        session: AsyncSession,
        *,
        rule: Fc2CodeRule,
        counts: ImportCounts,
        since: int | None,
        default_chat_id: int,
    ) -> None:
        self.session = session
        self.rule = rule
        self.counts = counts
        self.since = since
        self.default_chat_id = default_chat_id
        self.blacklisted: set[str] = set()

    # -- helpers ------------------------------------------------------------

    def in_scope(self, normalized: NormalizedContent) -> bool:
        if self.since is None:
            return True
        return int(normalized.canonical_key.split(":", 1)[1]) >= self.since

    async def content(
        self, normalized: NormalizedContent, *, title: str | None, source: str
    ) -> Content:
        existing = await self.session.scalar(
            select(Content).where(Content.canonical_key == normalized.canonical_key)
        )
        if existing is not None:
            return existing
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=normalized.canonical_key,
            display_code=normalized.display_code,
            title_ja=title,
            backfilled=False,
        )
        self.session.add(content)
        await self.session.flush()
        self.counts.stat(source).contents += 1
        self.counts.created_content_keys.append(normalized.canonical_key)
        return content

    async def accepted(
        self,
        content: Content,
        normalized: NormalizedContent,
        *,
        origin: AcceptedOrigin,
        source: str,
    ) -> bool:
        existing = await self.session.scalar(
            select(AcceptedEntry.id).where(AcceptedEntry.canonical_key == normalized.canonical_key)
        )
        if existing is not None:
            self.counts.stat(source).skipped += 1
            return False
        self.session.add(
            AcceptedEntry(
                canonical_key=normalized.canonical_key,
                content_id=content.id,
                origin=origin,
            )
        )
        await self.session.flush()
        self.counts.stat(source).accepted += 1
        return True

    async def release(
        self,
        content: Content,
        *,
        info_hash: str | None,
        title: str | None,
        published_at: datetime | None,
        source_name: str,
        source: str,
        raw: dict[str, Any] | None = None,
    ) -> None:
        if not info_hash:
            return
        existing = await self.session.scalar(select(Release).where(Release.info_hash == info_hash))
        if existing is not None:
            if existing.content_id != content.id:
                self.counts.add_conflict(
                    content.canonical_key, source, "info_hash_owned_by_other_content"
                )
            else:
                self.counts.stat(source).skipped += 1
            return
        self.session.add(
            Release(
                content_id=content.id,
                source=source_name,
                source_item_id=info_hash,
                magnet=f"magnet:?xt=urn:btih:{info_hash}",
                info_hash=info_hash,
                raw_title=title,
                published_at=published_at,
                raw_payload_json=raw or {"legacy_import": True},
            )
        )
        await self.session.flush()
        self.counts.stat(source).releases += 1

    # -- phases -------------------------------------------------------------

    async def import_blacklist(self, entries: list[LegacyBlacklist]) -> None:
        source = SOURCE_BLACKLIST
        seen: set[str] = set()
        for entry in entries:
            normalized = normalize_number(entry.number, self.rule)
            if normalized is None:
                self.counts.add_conflict(entry.number, source, "invalid_number")
                continue
            if not self.in_scope(normalized):
                self.counts.stat(source).skipped += 1
                continue
            if normalized.canonical_key in seen:
                self.counts.stat(source).skipped += 1
                continue
            seen.add(normalized.canonical_key)
            self.blacklisted.add(normalized.canonical_key)

            existing = await self.session.scalar(
                select(BlacklistEntry.id).where(
                    BlacklistEntry.canonical_key == normalized.canonical_key
                )
            )
            if existing is not None:
                self.counts.stat(source).skipped += 1
                continue
            content = await self.content(normalized, title=entry.title, source=source)
            self.session.add(
                BlacklistEntry(
                    canonical_key=normalized.canonical_key,
                    content_id=content.id,
                    reason=LEGACY_REASON,
                    created_by=LEGACY_ACTOR,
                    created_at=entry.added_at or utcnow(),
                )
            )
            await self.session.flush()
            self.counts.stat(source).blacklist += 1

    async def import_pushed(self, entries: list[LegacyPushed]) -> None:
        source = SOURCE_PUSHED
        seen: set[str] = set()
        for entry in entries:
            normalized = normalize_number(entry.number, self.rule)
            if normalized is None:
                self.counts.add_conflict(entry.number, source, "invalid_number")
                continue
            if not self.in_scope(normalized):
                self.counts.stat(source).skipped += 1
                continue
            if normalized.canonical_key in seen:
                self.counts.stat(source).skipped += 1
                continue
            seen.add(normalized.canonical_key)
            if normalized.canonical_key in self.blacklisted:
                self.counts.add_conflict(entry.number, source, "pushed_and_blacklisted")
                continue

            content = await self.content(normalized, title=entry.title, source=source)
            chat_id = entry.chat_id or self.default_chat_id
            existing = await self.session.scalar(
                select(Notification.id).where(
                    Notification.content_id == content.id,
                    Notification.bot_name == LEGACY_BOT,
                    Notification.chat_id == chat_id,
                )
            )
            if existing is not None:
                self.counts.stat(source).skipped += 1
                continue
            self.session.add(
                Notification(
                    content_id=content.id,
                    bot_name=LEGACY_BOT,
                    chat_id=chat_id,
                    poster_message_id=entry.message_ids[0],
                    media_message_ids_json=entry.message_ids[1:],
                    description_message_ids_json=[],
                    status=NotificationStatus.LEGACY,
                )
            )
            await self.session.flush()
            self.counts.stat(source).notifications += 1

    async def import_dead(self, entries: list[LegacyPushed]) -> None:
        """Seen but never delivered: no re-push, and deliberately no blacklist."""

        source = SOURCE_DEAD
        seen: set[str] = set()
        for entry in entries:
            normalized = normalize_number(entry.number, self.rule)
            if normalized is None:
                self.counts.add_conflict(entry.number, source, "invalid_number")
                continue
            if not self.in_scope(normalized):
                self.counts.stat(source).skipped += 1
                continue
            if normalized.canonical_key in seen:
                self.counts.stat(source).skipped += 1
                continue
            seen.add(normalized.canonical_key)
            if normalized.canonical_key in self.blacklisted:
                self.counts.add_conflict(entry.number, source, "dead_and_blacklisted")
                continue

            content = await self.content(normalized, title=entry.title, source=source)
            chat_id = entry.chat_id or self.default_chat_id
            existing = await self.session.scalar(
                select(Notification.id).where(
                    Notification.content_id == content.id,
                    Notification.bot_name == LEGACY_BOT,
                    Notification.chat_id == chat_id,
                )
            )
            if existing is not None:
                self.counts.stat(source).skipped += 1
                continue
            self.session.add(
                Notification(
                    content_id=content.id,
                    bot_name=LEGACY_BOT,
                    chat_id=chat_id,
                    status=NotificationStatus.LEGACY_DEAD,
                )
            )
            await self.session.flush()
            self.counts.stat(source).notifications += 1

    async def import_accepted(self, entries: list[LegacyAccepted], *, source: str) -> None:
        seen: set[str] = set()
        for entry in entries:
            normalized = normalize_number(entry.number, self.rule)
            if normalized is None:
                self.counts.add_conflict(entry.number, source, "invalid_number")
                continue
            if not self.in_scope(normalized):
                self.counts.stat(source).skipped += 1
                continue
            if normalized.canonical_key in seen:
                self.counts.stat(source).skipped += 1
                continue
            seen.add(normalized.canonical_key)
            if normalized.canonical_key in self.blacklisted:
                self.counts.add_conflict(entry.number, source, "accepted_and_blacklisted")
                continue

            content = await self.content(normalized, title=entry.title, source=source)
            await self.accepted(content, normalized, origin=LEGACY_ORIGIN, source=source)
            await self.release(
                content,
                info_hash=entry.info_hash,
                title=entry.title,
                published_at=entry.done_at,
                source_name=LEGACY_SOURCE,
                source=source,
            )

    async def import_transmission(self, entries: list[TransmissionTask]) -> None:
        source = SOURCE_TRANSMISSION
        seen: set[str] = set()
        for entry in entries:
            normalized = (
                normalize_number(entry.code, self.rule) if entry.code else None
            ) or normalize_from_name(entry.name, self.rule)
            if normalized is None:
                self.counts.add_conflict(
                    entry.name or entry.info_hash or "unknown", source, "missing_code"
                )
                continue
            if not self.in_scope(normalized):
                self.counts.stat(source).skipped += 1
                continue
            if normalized.canonical_key in seen:
                self.counts.stat(source).skipped += 1
                continue
            seen.add(normalized.canonical_key)
            if not entry.info_hash:
                self.counts.add_conflict(normalized.canonical_key, source, "missing_hash")
                continue
            if normalized.canonical_key in self.blacklisted:
                self.counts.add_conflict(
                    normalized.display_code or entry.name or "unknown",
                    source,
                    "transmission_and_blacklisted",
                )
                continue

            content = await self.content(normalized, title=entry.name, source=source)
            await self.accepted(content, normalized, origin=TRANSMISSION_ORIGIN, source=source)
            await self.release(
                content,
                info_hash=entry.info_hash,
                title=entry.name,
                published_at=None,
                source_name=TRANSMISSION_SOURCE,
                source=source,
                raw={"legacy_import": True, "download_dir": entry.download_dir},
            )


async def import_legacy(
    session: AsyncSession,
    *,
    rule: Fc2CodeRule,
    pushed: list[LegacyPushed],
    dead: list[LegacyPushed],
    blacklist: list[LegacyBlacklist],
    done: list[LegacyAccepted],
    claims: list[LegacyAccepted],
    pending: list[LegacyAccepted] | None = None,
    transmission: list[TransmissionTask] | None = None,
    since: int | None = None,
    default_chat_id: int = 0,
    counts: ImportCounts | None = None,
) -> ImportCounts:
    """Import every dataset into ``session`` (the caller owns the transaction)."""

    counts = counts or ImportCounts()
    importer = _Importer(
        session,
        rule=rule,
        counts=counts,
        since=since,
        default_chat_id=default_chat_id,
    )

    await importer.import_blacklist(blacklist)
    await importer.import_pushed(pushed)
    await importer.import_dead(dead)
    await importer.import_accepted(done, source=SOURCE_DONE)
    await importer.import_accepted(claims, source=SOURCE_CLAIMS)

    # ``fc2_done.jsonl.pending`` is the legacy "delete this card" queue, not a
    # download record: importing it would block content for no reason. The CLI only
    # counts its lines for the report.
    if transmission:
        await importer.import_transmission(transmission)
    return counts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import legacy fc2bot/torrentino state")
    parser.add_argument("--config", default=None, help="config.yaml (default: FC2_CONFIG)")
    parser.add_argument("--state", default=str(DEFAULTS["state"]))
    parser.add_argument("--blacklist", default=str(DEFAULTS["blacklist"]))
    parser.add_argument("--dead", default=str(DEFAULTS["dead"]))
    parser.add_argument("--done", default=str(DEFAULTS["done"]))
    parser.add_argument("--claims", default=str(DEFAULTS["claims"]))
    parser.add_argument("--pending", default=str(DEFAULTS["pending"]))
    parser.add_argument(
        "--transmission-export",
        default=None,
        help="JSON list of existing Transmission tasks to adopt",
    )
    parser.add_argument("--since", type=int, default=None, help="only numbers >= this")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument(
        "--transmission-migrate",
        choices=["finished", "incomplete"],
        default=None,
        help=(
            "adopt existing Transmission tasks: finished = let the organizer move "
            "them, incomplete = hand them to qBittorrent"
        ),
    )
    parser.add_argument(
        "--only-hashes",
        default=None,
        metavar="FILE",
        help="only migrate the hashes listed in FILE (one per line)",
    )
    parser.add_argument(
        "--rollback-contents",
        default=None,
        metavar="REPORT_JSON",
        help="delete the contents created by a previous import (unreferenced only)",
    )
    parser.add_argument(
        "--report",
        default=None,
        help="report path (default: import-report.json or migration-report.json)",
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> tuple[ImportCounts, dict[str, Any]]:
    settings = Settings.load(args.config)
    rule = Fc2CodeRule(settings.fc2.code_min_digits, settings.fc2.code_max_digits)

    pushed = parse_state(load_json(Path(args.state)))
    blacklist = parse_blacklist(load_json(Path(args.blacklist)))
    dead = parse_accepted(load_jsonl(Path(args.dead)))
    done = parse_accepted(load_jsonl(Path(args.done)))
    claims = parse_accepted(load_jsonl(Path(args.claims)))
    # Read-only: the pending file is a "delete this card" queue, never imported.
    pending = parse_accepted(load_jsonl(Path(args.pending)))
    transmission = (
        parse_transmission(load_json(Path(args.transmission_export)))
        if args.transmission_export
        else []
    )

    dead_records = [
        LegacyPushed(
            number=entry.number,
            title=entry.title,
            chat_id=None,
            message_ids=[],
        )
        for entry in dead
    ]

    started = time.monotonic()
    await migrate_async(settings.app.database_url)
    database = Database(settings.app.database_url)
    counts = ImportCounts(pending_lines_read=len(pending))
    try:
        try:
            async with database.transaction() as session:
                await import_legacy(
                    session,
                    rule=rule,
                    pushed=pushed,
                    dead=dead_records,
                    blacklist=blacklist,
                    done=done,
                    claims=claims,
                    pending=None,
                    transmission=transmission,
                    since=args.since,
                    default_chat_id=(
                        settings.telegram.allowed_user_ids[0]
                        if settings.telegram.allowed_user_ids
                        else 0
                    ),
                    counts=counts,
                )
                if args.dry_run:
                    raise _DryRunRollbackError
        except _DryRunRollbackError:
            pass
    finally:
        await database.dispose()

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "dry_run": bool(args.dry_run),
        "since": args.since,
        "sources": {
            "state": args.state,
            "blacklist": args.blacklist,
            "dead": args.dead,
            "done": args.done,
            "claims": args.claims,
            "pending": args.pending,
            "transmission_export": args.transmission_export,
        },
        "counts": counts.as_dict(),
        "conflicts": counts.conflicts,
    }
    return counts, summary


# ---------------------------------------------------------------------------
# Transmission migration (finished -> organize here, incomplete -> take over)
# ---------------------------------------------------------------------------


FINISHED_STATES = {"finished", "done", "complete", "completed", "seeding", "seed"}
#: Report groups: FC2-coded tasks stay separate from the generic (hash-identified) ones.
TRANSMISSION_GROUP = "transmission"
GENERIC_GROUP = "transmission_generic"
#: Transmission error codes we know how to interpret.
ERROR_TRACKER_REFUSED = 1  # "info hash is not authorized" - data may still be whole
ERROR_PIECE_CORRUPT = 3  # needs a force recheck before it can be trusted


@dataclass(slots=True)
class CategoryBucket:
    files: int = 0
    bytes: int = 0
    target: str = ""


@dataclass(slots=True)
class MigrationAction:
    number: str
    info_hash: str
    category: str | None
    target: str | None
    size_bytes: int | None
    action: str  # organize | takeover | skip
    reason: str | None = None
    #: ``transmission`` (FC2 code found) or ``transmission_generic`` (hash only).
    group: str = TRANSMISSION_GROUP


@dataclass(slots=True)
class MigrationReport:
    mode: str
    dry_run: bool
    actions: list[MigrationAction] = field(default_factory=list)
    conflicts: list[dict[str, str]] = field(default_factory=list)
    created: int = 0
    skipped: int = 0
    created_content_keys: list[str] = field(default_factory=list)
    warnings: list[dict[str, Any]] = field(default_factory=list)

    def planned(self) -> int:
        return sum(1 for action in self.actions if action.action != "skip")

    def bytes_total(self) -> int:
        return sum(action.size_bytes or 0 for action in self.actions if action.action != "skip")

    def by_group(self) -> dict[str, dict[str, int]]:
        """Per-group totals so generic tasks never hide inside the FC2 numbers."""

        grouped: dict[str, dict[str, int]] = {}
        for action in self.actions:
            bucket = grouped.setdefault(
                action.group, {"planned": 0, "skipped": 0, "bytes": 0}
            )
            if action.action == "skip":
                bucket["skipped"] += 1
            else:
                bucket["planned"] += 1
                bucket["bytes"] += action.size_bytes or 0
        return grouped

    def by_category(self, settings: Settings) -> dict[str, CategoryBucket]:
        grouped: dict[str, CategoryBucket] = {}
        for action in self.actions:
            if action.action == "skip" or not action.category:
                continue
            bucket = grouped.setdefault(
                action.category,
                CategoryBucket(target=str(settings.final_root(action.category))),
            )
            bucket.files += 1
            bucket.bytes += action.size_bytes or 0
        return grouped

    def add_skip(
        self,
        task: TransmissionTask,
        normalized: NormalizedContent | None,
        reason: str,
        *,
        group: str = TRANSMISSION_GROUP,
    ) -> None:
        number = (
            normalized.display_code
            if normalized is not None
            else (task.code or task.name or "unknown")
        )
        self.skipped += 1
        self.conflicts.append({"number": str(number), "reason": reason})
        self.actions.append(
            MigrationAction(
                number=str(number),
                info_hash=task.info_hash or "",
                category=None,
                target=None,
                size_bytes=task.size_bytes,
                action="skip",
                reason=reason,
                group=group,
            )
        )

    def as_dict(self, settings: Settings) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "dry_run": self.dry_run,
            "counts": {
                "planned": self.planned(),
                "created": self.created,
                "skipped": self.skipped,
                "conflicts": len(self.conflicts),
                "bytes_total": self.bytes_total(),
            },
            "by_group": self.by_group(),
            "by_category": {
                name: asdict(bucket) for name, bucket in self.by_category(settings).items()
            },
            "actions": [asdict(action) for action in self.actions],
            "conflicts": self.conflicts,
            "warnings": self.warnings,
            "created_content_keys": sorted(set(self.created_content_keys)),
        }


def effective_status(task: TransmissionTask) -> str:
    """Decide finished/incomplete from ``percent_done`` and the Transmission error.

    ``error == 1`` (tracker refused the hash) with a complete payload is fine: the
    data is there, only the tracker rejected it. ``error == 3`` (corrupt pieces) is
    only trustworthy after a force recheck, so a complete payload still counts as
    finished while anything below 1.0 falls back to incomplete.
    """

    percent = task.percent_done
    if percent is not None:
        complete = percent >= 1.0
        if task.error == ERROR_PIECE_CORRUPT:
            return "finished" if complete else "incomplete"
        if task.error == ERROR_TRACKER_REFUSED:
            return "finished" if complete else "incomplete"
        if complete:
            return "finished"
        return "incomplete"
    if task.status:
        return "finished" if task.status in FINISHED_STATES else "incomplete"
    return "unknown"


def filter_tasks_by_hashes(
    tasks: list[TransmissionTask], hashes: set[str]
) -> list[TransmissionTask]:
    if not hashes:
        return tasks
    return [task for task in tasks if task.info_hash in hashes]


def load_hashes(path: Path) -> set[str]:
    hashes: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        value = line.strip().lower()
        if not value or value.startswith("#"):
            continue
        hashes.add(value)
    return hashes


#: Torrent-name noise that says nothing about the content itself.
NAME_NOISE_KEYWORDS = (
    "1080p",
    "720p",
    "2160p",
    "480p",
    "4k",
    "fhd",
    "uhd",
    "x264",
    "x265",
    "h264",
    "h265",
    "hevc",
    "aac",
    "hdr",
    "60fps",
    "hd",
)
_BRACKET_RE = re.compile(r"[(\[【（]([^)\]】）]{0,80})[)\]】）]")
_EXTENSION_RE = re.compile(r"\.(mp4|mkv|avi|wmv|ts|m2ts|iso|rmvb|mov|flv)$", re.IGNORECASE)
#: Display code for a generic (non-FC2) task; also used for the target directory.
GENERIC_DISPLAY_CODE_MAX = 120


def _bracket_is_noise(inner: str) -> bool:
    lowered = inner.strip().lower()
    if not lowered:
        return True
    if any(keyword in lowered for keyword in NAME_NOISE_KEYWORDS):
        return True
    # Opaque site id such as ``fc4306363.45qi7skt``: one long token mixing letters
    # and digits. Real titles ("美女遊記") never look like that.
    return any(
        len(token) >= 10
        and any(char.isdigit() for char in token)
        and any(char.isalpha() for char in token)
        for token in lowered.split()
    )


def clean_torrent_name(name: object, *, limit: int = GENERIC_DISPLAY_CODE_MAX) -> str:
    """A readable label for a torrent that has no FC2 code.

    ``(HD1080P)(美女遊記)(fc4306363.45qi7skt)…`` → ``美女遊記…``: resolution tags,
    opaque ids and file extensions are dropped, meaningful bracketed titles stay.
    """

    text = str(name or "")
    text = _BRACKET_RE.sub(
        lambda match: "" if _bracket_is_noise(match.group(1)) else match.group(0), text
    )
    text = _EXTENSION_RE.sub("", text.strip())
    text = " ".join(text.replace(".", " ").replace("_", " ").split())
    text = text.strip(" -–—")
    if not text:
        return "unknown"
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def generic_identity(task: TransmissionTask) -> NormalizedContent:
    """Identity for a task without an FC2 code (spec 10.6 / 10.7).

    The info hash is the only stable identifier, so it becomes the canonical key —
    the ``infohash:`` prefix can never collide with ``fc2_ppv:`` — and the content
    is typed ``generic`` so it lands in ``_review`` instead of a real category.
    """

    info_hash = (task.info_hash or "").strip().lower()
    return NormalizedContent(
        content_type=ContentType.GENERIC,
        canonical_key=f"infohash:{info_hash}",
        display_code=clean_torrent_name(task.name),
    )


def resolve_category(task: TransmissionTask, normalized: NormalizedContent) -> str | None:
    # A generic (non-FC2) task is by definition unclassified: it goes to review so
    # nothing is filed into a category by guesswork (spec 10.7).
    if normalized.content_type is ContentType.GENERIC:
        return "review"
    if task.category:
        return task.category if task.category in CATEGORIES else None
    if normalized.content_type is ContentType.FC2:
        return "fc2"
    # Non-FC2 tasks have no automatic category rule; jav is the conservative default
    # and the report shows it per task so the operator can review before applying.
    return "jav"


async def migrate_transmission(
    session: AsyncSession,
    *,
    tasks: list[TransmissionTask],
    rule: Fc2CodeRule,
    settings: Settings,
    mode: str,
    report: MigrationReport | None = None,
) -> MigrationReport:
    """Create download jobs for existing Transmission tasks.

    ``finished``: status DOWNLOADED, downloader transmission — the monitor and the
    organizer then move the files with the normal code path and only remove the
    torrent (deleteFiles=false) after the move succeeded.

    ``incomplete``: status DOWNLOADING, downloader qbittorrent, remote save path taken
    from the export — the monitor takes over from there.
    """

    report = report or MigrationReport(mode=mode, dry_run=False)

    for task in tasks:
        normalized = (
            normalize_number(task.code, rule) if task.code else None
        ) or normalize_from_name(task.name, rule)
        generic = normalized is None
        if generic:
            # No FC2 code: fall back to the info hash, which is the only stable id
            # (spec 10.6). Without a hash the task cannot be identified at all.
            if not task.info_hash:
                report.add_skip(task, None, "missing_hash", group=GENERIC_GROUP)
                continue
            normalized = generic_identity(task)
        elif not task.info_hash:
            report.add_skip(task, normalized, "missing_hash")
            continue
        group = GENERIC_GROUP if generic else TRANSMISSION_GROUP

        derived = effective_status(task)
        if derived == "unknown":
            report.add_skip(task, normalized, "unknown_status", group=group)
            continue
        if derived != mode:
            report.add_skip(task, normalized, "status_mismatch", group=group)
            continue

        if mode == "incomplete" and not task.qb_save_path:
            report.add_skip(task, normalized, "missing_qb_save_path", group=group)
            continue
        if mode == "finished" and not task.download_dir:
            report.add_skip(task, normalized, "missing_download_dir", group=group)
            continue

        category = resolve_category(task, normalized)
        if category is None:
            report.add_skip(task, normalized, "unknown_category", group=group)
            continue

        target_name = normalized.display_code or task.name or normalized.canonical_key
        try:
            target = build_directory(
                settings.final_root(category),
                target_name,
                max_length=settings.storage.dir_name_max_length,
            )
        except Exception:
            report.add_skip(task, normalized, "invalid_target_name", group=group)
            continue

        existing_job = await session.scalar(
            select(DownloadJob.id).where(DownloadJob.info_hash == task.info_hash)
        )
        accepted = await session.scalar(
            select(AcceptedEntry.id).where(AcceptedEntry.canonical_key == normalized.canonical_key)
        )
        if existing_job is not None:
            report.add_skip(task, normalized, "job_exists", group=group)
            continue
        if accepted is not None:
            report.add_skip(task, normalized, "accepted_exists", group=group)
            continue

        content = await session.scalar(
            select(Content).where(Content.canonical_key == normalized.canonical_key)
        )
        if content is None:
            content = Content(
                content_type=normalized.content_type,
                canonical_key=normalized.canonical_key,
                display_code=normalized.display_code,
                title_ja=task.name,
                backfilled=False,
            )
            session.add(content)
            await session.flush()
            report.created_content_keys.append(normalized.canonical_key)

        release = await session.scalar(select(Release).where(Release.info_hash == task.info_hash))
        if release is None:
            release = Release(
                content_id=content.id,
                source=TRANSMISSION_SOURCE,
                source_item_id=task.info_hash,
                magnet=f"magnet:?xt=urn:btih:{task.info_hash}",
                info_hash=task.info_hash,
                raw_title=task.name,
                raw_payload_json={
                    "legacy_import": True,
                    "download_dir": task.download_dir,
                    "qb_save_path": task.qb_save_path,
                },
            )
            session.add(release)
            await session.flush()
        elif release.content_id != content.id:
            report.add_skip(task, normalized, "info_hash_owned_by_other_content", group=group)
            continue

        session.add(
            AcceptedEntry(
                canonical_key=normalized.canonical_key,
                content_id=content.id,
                origin=TRANSMISSION_ORIGIN,
            )
        )

        if mode == "finished":
            remote_content_path = (
                str(PurePosixPath(task.download_dir) / task.name)
                if task.download_dir and task.name
                else task.download_dir
            )
            job = DownloadJob(
                release_id=release.id,
                content_id=content.id,
                downloader=DownloaderName.TRANSMISSION,
                downloader_task_id=task.info_hash,
                info_hash=task.info_hash,
                status=DownloadStatus.DOWNLOADED,
                category=category,
                idempotency_key=f"download:{release.id}",
                remote_save_path=task.download_dir,
                remote_content_path=remote_content_path,
                submitted_at=utcnow(),
                completed_at=utcnow(),
            )
            action = "organize"
            if task.error == ERROR_PIECE_CORRUPT:
                report.warnings.append(
                    {
                        "number": normalized.display_code or normalized.canonical_key,
                        "warning": "error_3_piece_corrupt_verify_recheck",
                        "percent_done": task.percent_done,
                    }
                )
        else:
            job = DownloadJob(
                release_id=release.id,
                content_id=content.id,
                downloader=DownloaderName.QBITTORRENT,
                downloader_task_id=task.info_hash,
                info_hash=task.info_hash,
                status=DownloadStatus.DOWNLOADING,
                category=category,
                idempotency_key=f"download:{release.id}",
                remote_save_path=task.qb_save_path,
                submitted_at=utcnow(),
            )
            action = "takeover"

        session.add(job)
        await session.flush()
        report.created += 1
        report.actions.append(
            MigrationAction(
                number=normalized.display_code or normalized.canonical_key,
                info_hash=task.info_hash,
                category=category,
                target=str(target),
                size_bytes=task.size_bytes,
                action=action,
                group=group,
            )
        )

    return report


async def run_migration(args: argparse.Namespace) -> tuple[MigrationReport, dict[str, Any]]:
    if not args.transmission_export:
        raise LegacyImportError("--transmission-export is required with --transmission-migrate")

    settings = Settings.load(args.config)
    rule = Fc2CodeRule(settings.fc2.code_min_digits, settings.fc2.code_max_digits)
    tasks = parse_transmission(load_json(Path(args.transmission_export)))
    if args.only_hashes:
        selected = load_hashes(Path(args.only_hashes))
        tasks = filter_tasks_by_hashes(tasks, selected)

    report = MigrationReport(mode=args.transmission_migrate, dry_run=bool(args.dry_run))
    started = time.monotonic()
    await migrate_async(settings.app.database_url)
    database = Database(settings.app.database_url)
    try:
        try:
            async with database.transaction() as session:
                await migrate_transmission(
                    session,
                    tasks=tasks,
                    rule=rule,
                    settings=settings,
                    mode=args.transmission_migrate,
                    report=report,
                )
                if args.dry_run:
                    raise _DryRunRollbackError
        except _DryRunRollbackError:
            pass
    finally:
        await database.dispose()

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "source": args.transmission_export,
        **report.as_dict(settings),
    }
    return report, summary


async def delete_imported_contents(session: AsyncSession, keys: list[str]) -> tuple[int, int]:
    """Delete imported contents that nothing references any more."""

    deleted = 0
    kept = 0
    for key in keys:
        content = await session.scalar(select(Content).where(Content.canonical_key == key))
        if content is None:
            continue
        referenced = False
        for model, column in (
            (AcceptedEntry, AcceptedEntry.content_id),
            (BlacklistEntry, BlacklistEntry.content_id),
            (Notification, Notification.content_id),
            (Release, Release.content_id),
            (DownloadJob, DownloadJob.content_id),
        ):
            if await session.scalar(select(model.id).where(column == content.id)):
                referenced = True
                break
        if referenced:
            kept += 1
            continue
        await session.delete(content)
        deleted += 1
    await session.flush()
    return deleted, kept


async def rollback_contents(report_path: Path) -> tuple[int, int]:
    """CLI wrapper: read the report, then delete unreferenced imported contents."""

    payload = json.loads(report_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
    counts = payload.get("counts", payload)
    keys = counts.get("created_content_keys") or payload.get("created_content_keys") or []
    if not keys:
        return 0, 0

    settings = Settings.load(None)
    database = Database(settings.app.database_url)
    try:
        async with database.transaction() as session:
            return await delete_imported_contents(session, list(keys))
    finally:
        await database.dispose()


def _run_rollback_cli(args: argparse.Namespace) -> int:
    try:
        deleted, kept = asyncio.run(rollback_contents(Path(args.rollback_contents)))
    except Exception as exc:
        print(f"rollback failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"contents deleted={deleted} kept(referenced)={kept}")
    return 0


def _run_import_cli(args: argparse.Namespace) -> int:
    try:
        counts, summary = asyncio.run(run(args))
    except LegacyImportError as exc:
        print(f"import failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"import failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report_path = Path(args.report or "import-report.json")
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    totals = counts.totals()
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}created={totals['created']} skipped={totals['skipped']} "
        f"conflicts={totals['conflicts']} pending_lines={counts.pending_lines_read}"
    )
    for name, stats in counts.sources.items():
        print(
            f"  {name}: created={stats.created} "
            f"(contents={stats.contents} notifications={stats.notifications} "
            f"accepted={stats.accepted} releases={stats.releases} "
            f"blacklist={stats.blacklist}) skipped={stats.skipped} "
            f"conflicts={stats.conflicts}"
        )
    print(f"report: {report_path}")
    return 0


def _run_migration_cli(args: argparse.Namespace) -> int:
    try:
        report, summary = asyncio.run(run_migration(args))
    except LegacyImportError as exc:
        print(f"migration failed: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    report_path = Path(args.report or "migration-report.json")
    report_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    counts = summary["counts"]
    prefix = "[dry-run] " if args.dry_run else ""
    print(
        f"{prefix}mode={report.mode} planned={counts['planned']} "
        f"created={counts['created']} skipped={counts['skipped']} "
        f"conflicts={counts['conflicts']} bytes={counts['bytes_total']}"
    )
    for name, bucket in summary["by_category"].items():
        print(f"  {name}: files={bucket['files']} bytes={bucket['bytes']} -> {bucket['target']}")
    for action in report.actions:
        print(
            f"  {action.action:9s} {action.number:22s} {action.category or '-':8s} "
            f"{action.target or '-'} {action.reason or ''}"
        )
    print(f"report: {report_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.rollback_contents:
        return _run_rollback_cli(args)
    if args.transmission_migrate:
        return _run_migration_cli(args)
    return _run_import_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
