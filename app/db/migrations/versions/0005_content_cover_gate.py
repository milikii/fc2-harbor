"""contents.cover_status / cover_retry_count — publisher cover gate

A card without a cover is not pushed any more (``publisher.require_cover``), so the
publisher has to remember which contents are waiting for a cover and how many rounds
it already tried.

Both columns are plain (``cover_status`` has no CHECK constraint): adding one would
require rebuilding ``contents`` on SQLite, and six tables reference it by foreign
key. The value set is enforced in ``app.db.enums.CoverStatus``.

Revision ID: 0005_content_cover_gate
Revises: 0004_notification_delete_error
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_content_cover_gate"
down_revision: str | Sequence[str] | None = "0004_notification_delete_error"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "contents",
        sa.Column("cover_status", sa.String(length=32), nullable=False, server_default="pending"),
    )
    op.add_column(
        "contents",
        sa.Column("cover_retry_count", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    with op.batch_alter_table("contents") as batch_op:
        batch_op.drop_column("cover_retry_count")
        batch_op.drop_column("cover_status")
