"""add_rip_pid_to_jobs

Revision ID: 202601190000
Revises: 202601180000
Create Date: 2026-01-19 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202601190000"
down_revision: str | None = "202601180000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add rip_pid column to jobs table
    op.add_column("jobs", sa.Column("rip_pid", sa.Integer(), nullable=True))


def downgrade() -> None:
    # Drop column
    op.drop_column("jobs", "rip_pid")
