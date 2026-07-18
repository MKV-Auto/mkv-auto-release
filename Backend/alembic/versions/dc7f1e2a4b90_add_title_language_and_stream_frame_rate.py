"""Add language to disc_titles and frame_rate to disc_tracks

Revision ID: dc7f1e2a4b90
Revises: b2e1d3c4a567
Create Date: 2025-03-25 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "dc7f1e2a4b90"
down_revision: str | None = "b2e1d3c4a567"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("language_code", sa.String(), nullable=True))
    op.add_column("disc_titles", sa.Column("language", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("frame_rate", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("disc_tracks", "frame_rate")
    op.drop_column("disc_titles", "language")
    op.drop_column("disc_titles", "language_code")
