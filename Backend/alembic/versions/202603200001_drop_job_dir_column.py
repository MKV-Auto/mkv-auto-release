"""drop job_dir column from jobs

Job directory is now always computed from job ID via JobPaths.for_id().

Revision ID: 202603200001
Revises: 202603200000
Create Date: 2026-03-20 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202603200001"
down_revision: str | None = "202603200000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("jobs", "job_dir")


def downgrade() -> None:
    op.add_column("jobs", sa.Column("job_dir", sa.Text(), nullable=True))
