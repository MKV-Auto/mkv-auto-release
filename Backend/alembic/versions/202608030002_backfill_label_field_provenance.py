"""Backfill label-field provenance from resolved values

Revision ID: 202608030002
Revises: 202608030001
Create Date: 2026-08-03

Data half of the provenance split (DDL is 202608030001). Seeding rule,
from internal_docs/BLAST-RADIUS-title-state-v2.md:

- `auto_*` = current resolved value, for EVERY row. The current value
  becomes the automatic baseline — before this migration automation's
  opinion was overwritten in place, so a distinct historical auto value
  is unrecoverable anyway. This also gives user-retraction (PATCH null)
  a sensible fallback.
- `user_*` = current resolved value ONLY where `user_type IS NOT NULL`.
  A row whose type the user set was labeled by hand (the labeling flow
  sets name + type together), so its text fields keep user ownership —
  automation cannot overwrite them going forward. Rows without that
  signal seed as auto: forward-exact, backward-approximate, and the
  ambiguity is documented rather than guessed at.

Idempotent (NULL guards) and re-runnable — a restarted container may
re-enter mid-migration. Resolved columns are never written here, so a
crash mid-backfill loses nothing.
"""
from alembic import op


revision = '202608030002'
down_revision = '202608030001'
branch_labels = None
depends_on = None

_FIELDS = ("title", "edition", "description", "season", "episode")


def upgrade():
    for f in _FIELDS:
        op.execute(
            f"UPDATE disc_titles SET auto_{f} = {f} "
            f"WHERE {f} IS NOT NULL AND auto_{f} IS NULL"
        )
        op.execute(
            f"UPDATE disc_titles SET user_{f} = {f} "
            f"WHERE user_type IS NOT NULL AND {f} IS NOT NULL AND user_{f} IS NULL"
        )


def downgrade():
    # Exact inverse of the backfill: clear the provenance columns. The
    # resolved columns were never touched, so no data is lost.
    for f in _FIELDS:
        op.execute(f"UPDATE disc_titles SET auto_{f} = NULL, user_{f} = NULL")
