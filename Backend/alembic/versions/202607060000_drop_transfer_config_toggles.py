"""Drop three redundant toggles from transfer_configs.

Revision ID: 202607060000
Revises: 202606280001
Create Date: 2026-07-06

Drops the ``cleanup_source``, ``enable_deduplication``, and
``enable_notifications`` columns from ``transfer_configs``. All three
duplicated behaviour already reachable elsewhere in the app; keeping
them created UX drift.

- ``cleanup_source`` was a "clean up now vs let the reconciler catch
  it in <=1h" knob. The periodic ``reconcile_job_mkv_cleanup`` task in
  ``workers/tasks.py`` already cleans every completed/failed job
  unconditionally, so the toggle only controlled *timing*, not
  behaviour. Cleanup is now always synchronous after a successful,
  verified transfer.
- ``enable_deduplication`` folds into ``conflict_resolution``: the new
  semantic is "if the strategy is ``skip``, run the pre-flight hash
  check; otherwise a hash check is pointless (overwrite/fail/rename
  don't need it)".
- ``enable_notifications`` duplicated the global Settings -> Notifications
  page (which already has per-category x per-channel controls). Kept as
  UX noise; drop the per-config kill switch.

Idempotent shape mirrors #612 / #543 style: ``ALTER TABLE ... DROP
COLUMN IF EXISTS`` so re-running (or partially-applied envs) heals
forward cleanly.
"""
from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision = "202607060000"
down_revision = "202606280001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE transfer_configs DROP COLUMN IF EXISTS cleanup_source;")
    op.execute("ALTER TABLE transfer_configs DROP COLUMN IF EXISTS enable_deduplication;")
    op.execute("ALTER TABLE transfer_configs DROP COLUMN IF EXISTS enable_notifications;")


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'transfer_configs' AND column_name = 'cleanup_source'
            ) THEN
                ALTER TABLE transfer_configs
                ADD COLUMN cleanup_source BOOLEAN NOT NULL DEFAULT false;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'transfer_configs' AND column_name = 'enable_deduplication'
            ) THEN
                ALTER TABLE transfer_configs
                ADD COLUMN enable_deduplication BOOLEAN NOT NULL DEFAULT false;
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'transfer_configs' AND column_name = 'enable_notifications'
            ) THEN
                ALTER TABLE transfer_configs
                ADD COLUMN enable_notifications BOOLEAN NOT NULL DEFAULT true;
            END IF;
        END
        $$;
        """
    )
