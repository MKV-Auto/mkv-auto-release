"""Add ``user_edited_at`` to ``discs``.

Revision ID: 202607290000
Revises: 202607280000
Create Date: 2026-07-29

Stamped by the human edit paths only — the title PATCH endpoints and the disc
metadata PATCH — never by pipeline writes (file paths, stages, scan updates).
That makes it a precise "the user corrected something" signal.

Its consumer is TheDiscDB dirty detection: a DiscDB *hit* whose data the user
then edited means the local copy is better than upstream, so the disc becomes
eligible for export again as an update submission. Without the stamp, hits are
permanently excluded and corrections never flow back upstream.

Pure expand: one nullable column, no backfill — the past holds no record of
which edits were human, so pretending otherwise would invent data.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "202607290000"
down_revision = "202607280000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE discs ADD COLUMN IF NOT EXISTS user_edited_at TIMESTAMP WITH TIME ZONE"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE discs DROP COLUMN IF EXISTS user_edited_at")
