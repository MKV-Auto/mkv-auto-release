"""Add segment_uid to disc_titles.

Revision ID: 202605300000
Revises: 202605290000
Create Date: 2026-05-30

Issue #448 — capture Matroska Segment UID at postprocess.

Adds ``disc_titles.segment_uid`` (nullable String, indexed). Populated by
``workers.tasks.resume_postprocess`` via ``core.mkv_identity.read_segment_uid``
once the file is in its final muxed form. NULL is expected on every legacy
row produced before this migration; the downstream consumers
(transient/-drop 5b'b src==dest shortcut, #449 self-healing reattach) treat
NULL as "fall back to heuristic match".
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202605300000"
down_revision: str | None = "202605290000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "disc_titles",
        sa.Column("segment_uid", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_disc_titles_segment_uid",
        "disc_titles",
        ["segment_uid"],
    )


def downgrade() -> None:
    op.drop_index("ix_disc_titles_segment_uid", table_name="disc_titles")
    op.drop_column("disc_titles", "segment_uid")
