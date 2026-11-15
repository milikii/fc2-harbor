"""contents.backfilled — silent first-run backfill (spec 6.2 / 9)

Contents written during the initial lookback window are flagged so the push queue
skips them; only contents discovered after the first successful round are pushed.

Revision ID: 0002_content_backfilled
Revises: 0001_initial_schema
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_content_backfilled"
down_revision: str | Sequence[str] | None = "0001_initial_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("contents") as batch_op:
        batch_op.add_column(
            sa.Column("backfilled", sa.Boolean(), nullable=False, server_default=sa.false())
        )
    op.create_index("ix_contents_backfilled", "contents", ["backfilled"])

    # Everything that already exists was discovered by the pre-fix poller, i.e. by
    # the first-run backfill. Mark it silent so an upgrade does not suddenly push
    # the whole backlog; contents that were already notified or accepted are
    # excluded from the push queue anyway.
    op.execute(sa.text("UPDATE contents SET backfilled = 1"))


def downgrade() -> None:
    op.drop_index("ix_contents_backfilled", table_name="contents")
    with op.batch_alter_table("contents") as batch_op:
        batch_op.drop_column("backfilled")
