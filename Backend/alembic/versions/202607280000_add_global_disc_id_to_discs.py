"""Add ``global_disc_id`` to ``discs``.

Revision ID: 202607280000
Revises: 202607060000
Create Date: 2026-07-28

TheDiscDB's ``GlobalDiscId`` is the AACS disc ID — ``SHA1`` of the disc's
unencrypted ``AACS/Unit_Key_RO.inf``, uppercase hex. It identifies a *pressing*
globally, where our ``content_hash`` identifies it by file layout, and upstream
wants both.

Pure expand: one nullable column, no backfill here. The value cannot be derived
from anything already in the database — it only exists on the physical disc — so
a migration-time backfill is impossible by construction. Instead the scan writes
it whenever a disc is in the drive and the column is empty, which means an
existing library fills in on its own as discs get re-inserted.

Nullable and staying that way: DVDs have no AACS directory at all, and a disc
whose filesystem cannot be mounted will never get one.
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "202607280000"
down_revision = "202607060000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS so a partially-applied environment heals forward, matching
    # the idempotent shape used by the surrounding migrations.
    op.execute("ALTER TABLE discs ADD COLUMN IF NOT EXISTS global_disc_id VARCHAR")


def downgrade() -> None:
    op.execute("ALTER TABLE discs DROP COLUMN IF EXISTS global_disc_id")
