"""Rename info_label to info_title on releases and discs

Revision ID: c1d2e3f4a5b6
Revises: b6f4a7c12d34
Create Date: 2025-03-24 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: str | None = "b6f4a7c12d34"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns
    op.add_column("releases", sa.Column("info_title", sa.Text(), nullable=True))
    op.add_column("discs", sa.Column("info_title", sa.Text(), nullable=True))
    # Copy existing data
    op.execute("UPDATE releases SET info_title = info_label WHERE info_label IS NOT NULL")
    op.execute("UPDATE discs SET info_title = info_label WHERE info_label IS NOT NULL")
    # Drop old columns
    op.drop_column("releases", "info_label")
    op.drop_column("discs", "info_label")


def downgrade() -> None:
    op.add_column("discs", sa.Column("info_label", sa.Text(), nullable=True))
    op.add_column("releases", sa.Column("info_label", sa.Text(), nullable=True))
    op.execute("UPDATE releases SET info_label = info_title WHERE info_title IS NOT NULL")
    op.execute("UPDATE discs SET info_label = info_title WHERE info_title IS NOT NULL")
    op.drop_column("discs", "info_title")
    op.drop_column("releases", "info_title")
