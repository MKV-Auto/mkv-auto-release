"""add edition column to disc_titles

Revision ID: 202601270000
Revises: 202601241200
Create Date: 2026-01-27 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202601270000"
down_revision: str | None = "202601241200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("edition", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("disc_titles", "edition")
