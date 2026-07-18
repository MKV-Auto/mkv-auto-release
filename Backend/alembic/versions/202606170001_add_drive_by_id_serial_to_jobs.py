"""Add drive_by_id_serial column to jobs.

Revision ID: 202606170001
Revises: 202606010000
Create Date: 2026-06-17

Part of the multi-drive stable-identity work (#540). New nullable column
stores the ``/dev/disk/by-id/`` serial of the drive the job was created
against — the primary stable identifier replacing the volatile
``mount_point`` for restart-during-rip recovery and identity-swap
detection.

NULL means the job was either created before this migration or against
a drive that resolved only via the by-path / sysfs fallback. The
gatekeeper treats NULL as multi-drive-unsafe (see ``core.drive_policy``).
The downgrade simply drops the column; existing rip flows that fall back
to ``mount_point`` continue to work, just without the new safety net.

Indexed because recovery and identity-swap detection both query by it.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202606170001"
down_revision: str | None = "202606010000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("drive_by_id_serial", sa.String(), nullable=True),
    )
    op.create_index(
        "ix_jobs_drive_by_id_serial",
        "jobs",
        ["drive_by_id_serial"],
    )


def downgrade() -> None:
    op.drop_index("ix_jobs_drive_by_id_serial", table_name="jobs")
    op.drop_column("jobs", "drive_by_id_serial")
