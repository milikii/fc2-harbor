"""contents.title_zh / description translation binding — bilingual cards

Three nullable columns:

* ``title_zh`` — the Chinese title. ``title_ja`` stays the original; a failed
  translation leaves this NULL and the card simply omits the Chinese line.
* ``description_zh_source_hash`` — SHA-256 of the ``description_ja`` that the
  cached ``description_zh`` was translated from. When the description changes the
  digest no longer matches and the text is translated again instead of serving a
  stale translation.
* ``description_source`` — the provider id that supplied ``description_ja``
  (``Fc2``, ``Nyaa``, …), so a non-official uploader post can be labelled as such
  and the publisher reads the same selected text the enrichment chose.

All three are plain nullable columns: no CHECK constraints, so SQLite does not
need to rebuild ``contents`` (eight tables reference it by foreign key).

Revision ID: 0006_title_zh_translation_binding
Revises: 0005_content_cover_gate
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_title_zh_translation_binding"
down_revision: str | Sequence[str] | None = "0005_content_cover_gate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("contents", sa.Column("title_zh", sa.Text(), nullable=True))
    op.add_column(
        "contents",
        sa.Column("description_zh_source_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "contents",
        sa.Column("description_source", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    with op.batch_alter_table("contents") as batch_op:
        batch_op.drop_column("description_source")
        batch_op.drop_column("description_zh_source_hash")
        batch_op.drop_column("title_zh")
