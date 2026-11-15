"""legacy import enum values — accepted_origin 'legacy_import', status 'legacy'

The one-off importer from the legacy fc2bot/torrentino state marks rows so they can
be rolled back precisely, and so the push queue never re-delivers a card the old
system already sent. SQLite stores enums as VARCHAR + CHECK, so the constraint has
to be recreated.

Revision ID: 0003_legacy_import_enums
Revises: 0002_content_backfilled
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_legacy_import_enums"
down_revision: str | Sequence[str] | None = "0002_content_backfilled"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ORIGINS = (
    "subscription_button",
    "manual",
    "admin",
    "legacy_import",
    "legacy:transmission",
)
STATUSES = ("pending", "sent", "deleted", "failed", "legacy", "legacy:dead")


def _enum(name: str, *values: str) -> sa.Enum:
    return sa.Enum(*values, name=name, native_enum=False, create_constraint=True, length=32)


def upgrade() -> None:
    with op.batch_alter_table("accepted_entries") as batch_op:
        batch_op.alter_column(
            "origin",
            existing_type=_enum("accepted_origin", *ORIGINS[:3]),
            type_=_enum("accepted_origin", *ORIGINS),
            existing_nullable=False,
        )
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_enum("notification_status", *STATUSES[:4]),
            type_=_enum("notification_status", *STATUSES),
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute(sa.text("DELETE FROM notifications WHERE status LIKE 'legacy%'"))
    op.execute(sa.text("DELETE FROM accepted_entries WHERE origin LIKE 'legacy%'"))
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_enum("notification_status", *STATUSES),
            type_=_enum("notification_status", *STATUSES[:4]),
            existing_nullable=False,
        )
    with op.batch_alter_table("accepted_entries") as batch_op:
        batch_op.alter_column(
            "origin",
            existing_type=_enum("accepted_origin", *ORIGINS),
            type_=_enum("accepted_origin", *ORIGINS[:3]),
            existing_nullable=False,
        )
