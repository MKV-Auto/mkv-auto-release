"""add metadata_scan to disc_titles

Revision ID: 202601271000
Revises: 202601270000
Create Date: 2026-01-27 10:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202601271000"
down_revision: str | None = "202601270000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("metadata_scan", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("disc_titles", "metadata_scan")
