"""notifications.delete_error — why a card could not be deleted

A card that Telegram refuses to delete (older than 48 hours, already gone, bot
without rights) used to leave only a log line. The reason is now stored on the
notification row so the operator can see it in the database.

Revision ID: 0004_notification_delete_error
Revises: 0003_legacy_import_enums
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_notification_delete_error"
down_revision: str | Sequence[str] | None = "0003_legacy_import_enums"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications", sa.Column("delete_error", sa.String(length=255), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("notifications") as batch_op:
        batch_op.drop_column("delete_error")
