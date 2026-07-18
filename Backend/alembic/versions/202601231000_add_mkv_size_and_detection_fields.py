"""add mkv_size and detection fields to disc_titles

Revision ID: 202601231000
Revises: 202601230000
Create Date: 2026-01-23 10:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202601231000"
down_revision: str | None = "202601230000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("mkv_size", sa.BigInteger(), nullable=True))
    op.add_column("disc_titles", sa.Column("detection_flags", sa.JSON(), nullable=True))
    op.add_column("disc_titles", sa.Column("detection_confidence", sa.Float(), nullable=True))
    op.add_column(
        "disc_titles",
        sa.Column("detection_warning", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    op.drop_column("disc_titles", "detection_warning")
    op.drop_column("disc_titles", "detection_confidence")
    op.drop_column("disc_titles", "detection_flags")
    op.drop_column("disc_titles", "mkv_size")
