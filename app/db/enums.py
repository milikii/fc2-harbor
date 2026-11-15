"""Enumerations shared by the ORM models, schemas and services.

Spec references: section 12 (database columns), section 13 (download state
machine), section 7.3 (censorship tags), section 10 (categories).

All enums are ``str`` enums so they serialise to plain strings in SQLite JSON
columns, logs and Telegram callback payloads.
"""

from __future__ import annotations

from enum import StrEnum


class ContentType(StrEnum):
    """Spec 6.3 / 12.1."""

    FC2 = "fc2"
    JAV = "jav"
    GENERIC = "generic"


class Censorship(StrEnum):
    """Spec 7.3 — unified censorship tag."""

    CENSORED = "censored"
    UNCENSORED = "uncensored"
    THIN = "thin"
    UNKNOWN = "unknown"


class MetadataStatus(StrEnum):
    """Aggregation progress of a content row."""

    PENDING = "pending"
    PARTIAL = "partial"
    COMPLETE = "complete"
    FAILED = "failed"


class CoverStatus(StrEnum):
    """Whether a content may be pushed (``publisher.require_cover``)."""

    #: Never evaluated yet.
    PENDING = "pending"
    #: Enrichment ran, no cover asset; the publisher will retry next round.
    PENDING_COVER = "pending_cover"
    #: A cover exists, cards may go out.
    READY = "ready"
    #: Retry limit reached; no further attempts, no card.
    GIVE_UP = "give_up"


class AssetRole(StrEnum):
    """Spec 12.4 — assets.role."""

    COVER = "cover"
    PREVIEW = "preview"


class AcceptedOrigin(StrEnum):
    """Spec 12.5 — accepted_entries.origin."""

    SUBSCRIPTION_BUTTON = "subscription_button"
    MANUAL = "manual"
    ADMIN = "admin"
    #: Imported from the legacy fc2bot/torrentino state.
    LEGACY_IMPORT = "legacy_import"
    #: Adopted from an existing Transmission task (still seeding elsewhere).
    LEGACY_TRANSMISSION = "legacy:transmission"


class DownloaderName(StrEnum):
    """Spec 12.7 — download_jobs.downloader."""

    QBITTORRENT = "qbittorrent"
    TRANSMISSION = "transmission"


class DownloadStatus(StrEnum):
    """Spec 13 — download job lifecycle.

    ``DISCOVERED`` … ``TASK_REMOVED`` is the happy path; ``BLOCKED``,
    ``FAILED`` and ``CANCELLED`` are side states.
    """

    DISCOVERED = "DISCOVERED"
    NOTIFIED = "NOTIFIED"
    ACCEPTED = "ACCEPTED"
    SUBMITTED = "SUBMITTED"
    DOWNLOADING = "DOWNLOADING"
    DOWNLOADED = "DOWNLOADED"
    ORGANIZING = "ORGANIZING"
    ORGANIZED = "ORGANIZED"
    TASK_REMOVED = "TASK_REMOVED"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class NotificationStatus(StrEnum):
    """Spec 12.8 — notifications.status."""

    PENDING = "pending"
    SENT = "sent"
    DELETED = "deleted"
    FAILED = "failed"
    #: Card already delivered by the legacy system; never re-pushed.
    LEGACY = "legacy"
    #: Legacy record that was seen but never delivered (download failed); also
    #: never re-pushed, but not blacklisted either.
    LEGACY_DEAD = "legacy:dead"


class GateReason(StrEnum):
    """Spec 11.1 — the exact order of admission checks."""

    ALLOWED = "allowed"
    BLACKLISTED = "blacklisted"
    ALREADY_ACCEPTED = "already_accepted"
    INFO_HASH_IN_JOBS = "info_hash_in_jobs"
    SOURCE_ITEM_ALREADY_PROCESSED = "source_item_already_processed"
    INVALID_CANDIDATE = "invalid_candidate"


class MoveStage(StrEnum):
    """Stage names used when an organize attempt fails (spec 16.3)."""

    VALIDATE = "validate"
    CREATE_DIR = "create_dir"
    RENAME = "rename"
    COPY = "copy"
    FSYNC = "fsync"
    VERIFY = "verify"
    CONFLICT = "conflict"
    CLEANUP = "cleanup"
