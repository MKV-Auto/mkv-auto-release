"""add_rip_phase_to_jobs

Revision ID: 202603060000
Revises: 202602150000
Create Date: 2026-03-06 00:00:00.000000

Add rip_phase column to jobs for declarative copy vs verification stage (issue #69).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202603060000"
down_revision: Union[str, None] = "202602150000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("rip_phase", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "rip_phase")
