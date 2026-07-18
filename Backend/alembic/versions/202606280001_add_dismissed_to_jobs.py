"""Add dismissed column to jobs.

Revision ID: 202606280001
Revises: 202606170001
Create Date: 2026-06-28

The ``dismissed`` flag on Job (``models.py:357``) backs the user "dismiss
this job from the carousel" affordance from #543, but the column was
only ever added to the SQLAlchemy model — no Alembic migration ever
created it. Dev databases that had the column happened to receive it
via a manual ``ALTER TABLE``; fresh 1.0.0 installs and any upgrade
without that manual step hit ``UndefinedColumn`` from
``_resolve_in_drive_job_for_disc`` (websockets.py:59), which 500s
``GET /coordinator/initial-state`` and the periodic stale-job cleanup.

Surfaced during v1.0.0 smoke testing in the rebuilt release container:
``/api/coordinator/initial-state`` returned 500 with
``column jobs.dismissed does not exist``.

Idempotent: ``CREATE COLUMN IF NOT EXISTS``-shaped via a ``DO $$``
block so an environment that already has the column (manually added,
or partially-applied schema) heals forward cleanly. Same posture as
the #612 fix on the performance-indexes migration.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "202606280001"
down_revision = "202606170001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'jobs' AND column_name = 'dismissed'
            ) THEN
                ALTER TABLE jobs
                ADD COLUMN dismissed BOOLEAN NOT NULL DEFAULT false;
            END IF;
        END
        $$;
        """
    )


def downgrade() -> None:
    op.execute("ALTER TABLE jobs DROP COLUMN IF EXISTS dismissed;")
