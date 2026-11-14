"""SQLAlchemy 2.x models — the schema from spec section 12.

Conventions
-----------
* Primary keys are UUID strings (portable between SQLite and a future Postgres).
* Uniqueness that the content gate relies on (spec 11.2) lives in the database,
  not only in application code: ``contents.canonical_key``,
  ``accepted_entries.canonical_key``, ``blacklist_entries.canonical_key``,
  ``releases(source, source_item_id)``, non-null ``releases.info_hash`` and
  ``download_jobs.idempotency_key`` are all unique.
* Relationships are ``lazy="raise"``: in an async session an implicit lazy load
  is a bug, so callers must use ``selectinload`` explicitly.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.db.enums import (
    AcceptedOrigin,
    AssetRole,
    Censorship,
    ContentType,
    DownloaderName,
    DownloadStatus,
    MetadataStatus,
    NotificationStatus,
)


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp; the single clock used by the app."""

    return datetime.now(UTC)


def new_uuid() -> str:
    return str(uuid.uuid4())


NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def enum_column(enum_cls: type[Enum], name: str, length: int = 32) -> SAEnum:
    """Store enums as their ``value`` with a CHECK constraint (portable, readable)."""

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=length,
        values_callable=lambda members: [str(member.value) for member in members],
        validate_strings=True,
        create_constraint=True,
    )


class Content(Base):
    """Spec 12.1 — one row per real content (one FC2 number)."""

    __tablename__ = "contents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_type: Mapped[ContentType] = mapped_column(
        enum_column(ContentType, "content_type"), nullable=False
    )
    canonical_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    display_code: Mapped[str | None] = mapped_column(String(255))
    title_ja: Mapped[str | None] = mapped_column(Text)
    description_ja: Mapped[str | None] = mapped_column(Text)
    description_zh: Mapped[str | None] = mapped_column(Text)
    censorship: Mapped[Censorship] = mapped_column(
        enum_column(Censorship, "censorship"), nullable=False, default=Censorship.UNKNOWN
    )
    official_url: Mapped[str | None] = mapped_column(String(1024))
    metadata_status: Mapped[MetadataStatus] = mapped_column(
        enum_column(MetadataStatus, "metadata_status"),
        nullable=False,
        default=MetadataStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    releases: Mapped[list[Release]] = relationship(
        back_populates="content", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    assets: Mapped[list[Asset]] = relationship(
        back_populates="content", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )
    provider_results: Mapped[list[ProviderResult]] = relationship(
        back_populates="content", lazy="raise", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_contents_content_type", "content_type"),)


class Release(Base):
    """Spec 12.2 — one source listing (Nyaa item, manual magnet, …)."""

    __tablename__ = "releases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="CASCADE"), nullable=False
    )
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_item_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    magnet: Mapped[str | None] = mapped_column(Text)
    info_hash: Mapped[str | None] = mapped_column(String(40))
    raw_title: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_payload_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    content: Mapped[Content] = relationship(back_populates="releases", lazy="raise")
    jobs: Mapped[list[DownloadJob]] = relationship(back_populates="release", lazy="raise")

    __table_args__ = (
        UniqueConstraint("source", "source_item_id", name="uq_releases_source_item_id"),
        Index("ix_releases_content_id", "content_id"),
        # Non-empty info hash is unique (spec 11.2). Partial index so that many
        # releases without a hash stay allowed.
        Index(
            "uq_releases_info_hash",
            "info_hash",
            unique=True,
            sqlite_where=text("info_hash IS NOT NULL"),
            postgresql_where=text("info_hash IS NOT NULL"),
        ),
    )


class ProviderResult(Base):
    """Spec 12.3 — one row per provider attempt, including failures."""

    __tablename__ = "provider_results"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    asset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    content: Mapped[Content] = relationship(back_populates="provider_results", lazy="raise")

    __table_args__ = (Index("ix_provider_results_content_provider", "content_id", "provider"),)


class Asset(Base):
    """Spec 12.4 — cached cover/preview images."""

    __tablename__ = "assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[AssetRole] = mapped_column(enum_column(AssetRole, "asset_role"), nullable=False)
    original_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(2048))
    normalized_url: Mapped[str | None] = mapped_column(String(2048))
    cdn_host: Mapped[str | None] = mapped_column(String(255))
    proxy_profile: Mapped[str | None] = mapped_column(String(64))
    sha256: Mapped[str | None] = mapped_column(String(64))
    local_path: Mapped[str | None] = mapped_column(String(1024))
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    telegram_bot: Mapped[str | None] = mapped_column(String(32))
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    content: Mapped[Content] = relationship(back_populates="assets", lazy="raise")

    __table_args__ = (
        Index("ix_assets_content_role_position", "content_id", "role", "position"),
        # Spec 7.2 rule 6: dedupe by normalised URL and by content SHA-256.
        Index(
            "uq_assets_content_normalized_url",
            "content_id",
            "normalized_url",
            unique=True,
            sqlite_where=text("normalized_url IS NOT NULL"),
            postgresql_where=text("normalized_url IS NOT NULL"),
        ),
        Index(
            "uq_assets_content_sha256",
            "content_id",
            "sha256",
            unique=True,
            sqlite_where=text("sha256 IS NOT NULL"),
            postgresql_where=text("sha256 IS NOT NULL"),
        ),
    )


class AcceptedEntry(Base):
    """Spec 12.5 — canonical keys the user accepted; never re-pushed."""

    __tablename__ = "accepted_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    canonical_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="RESTRICT"), nullable=False
    )
    origin: Mapped[AcceptedOrigin] = mapped_column(
        enum_column(AcceptedOrigin, "accepted_origin"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (Index("ix_accepted_entries_content_id", "content_id"),)


class BlacklistEntry(Base):
    """Spec 12.6 — canonical keys the user rejected; highest gate priority."""

    __tablename__ = "blacklist_entries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    canonical_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="SET NULL"))
    reason: Mapped[str | None] = mapped_column(String(255))
    created_by: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )


class DownloadJob(Base):
    """Spec 12.7 — one download attempt for one release."""

    __tablename__ = "download_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    release_id: Mapped[str] = mapped_column(
        ForeignKey("releases.id", ondelete="RESTRICT"), nullable=False
    )
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="RESTRICT"), nullable=False
    )
    downloader: Mapped[DownloaderName] = mapped_column(
        enum_column(DownloaderName, "downloader_name"), nullable=False
    )
    downloader_task_id: Mapped[str | None] = mapped_column(String(255))
    info_hash: Mapped[str | None] = mapped_column(String(40))
    status: Mapped[DownloadStatus] = mapped_column(
        enum_column(DownloadStatus, "download_status"),
        nullable=False,
        default=DownloadStatus.DISCOVERED,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    #: ``download:<release_id>`` — spec 11.2 idempotency key.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    remote_save_path: Mapped[str | None] = mapped_column(String(1024))
    remote_content_path: Mapped[str | None] = mapped_column(String(1024))
    local_content_path: Mapped[str | None] = mapped_column(String(1024))
    final_path: Mapped[str | None] = mapped_column(String(1024))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    organized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    removed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    release: Mapped[Release] = relationship(back_populates="jobs", lazy="raise")

    __table_args__ = (
        Index("ix_download_jobs_status", "status"),
        Index("ix_download_jobs_info_hash", "info_hash"),
        Index("ix_download_jobs_content_id", "content_id"),
    )


class Notification(Base):
    """Spec 12.8 — every Telegram message id, so a group can be deleted."""

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str] = mapped_column(
        ForeignKey("contents.id", ondelete="CASCADE"), nullable=False
    )
    bot_name: Mapped[str] = mapped_column(String(32), nullable=False)
    chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    poster_message_id: Mapped[int | None] = mapped_column(BigInteger)
    description_message_ids_json: Mapped[list[int] | None] = mapped_column(JSON)
    media_message_ids_json: Mapped[list[int] | None] = mapped_column(JSON)
    status: Mapped[NotificationStatus] = mapped_column(
        enum_column(NotificationStatus, "notification_status"),
        nullable=False,
        default=NotificationStatus.PENDING,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (Index("ix_notifications_content_id", "content_id"),)


class SourceCursor(Base):
    """Spec 12.9 / 6.2 — per-source polling cursor and failure counter."""

    __tablename__ = "source_cursors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    source: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    cursor_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class DecisionEvent(Base):
    """Spec 12.9 — audit trail for accept/reject/retry/status changes."""

    __tablename__ = "decision_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    content_id: Mapped[str | None] = mapped_column(ForeignKey("contents.id", ondelete="SET NULL"))
    canonical_key: Mapped[str | None] = mapped_column(String(255))
    release_id: Mapped[str | None] = mapped_column(ForeignKey("releases.id", ondelete="SET NULL"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("download_jobs.id", ondelete="SET NULL"))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str | None] = mapped_column(String(32))
    actor: Mapped[str | None] = mapped_column(String(64))
    reason: Mapped[str | None] = mapped_column(String(255))
    error_code: Mapped[str | None] = mapped_column(String(64))
    latency_ms: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    __table_args__ = (
        Index("ix_decision_events_content_id", "content_id"),
        Index("ix_decision_events_job_id", "job_id"),
        Index("ix_decision_events_created_at", "created_at"),
    )


__all__ = [
    "AcceptedEntry",
    "Asset",
    "Base",
    "BlacklistEntry",
    "Content",
    "DecisionEvent",
    "DownloadJob",
    "Notification",
    "ProviderResult",
    "Release",
    "SourceCursor",
    "new_uuid",
    "utcnow",
]
