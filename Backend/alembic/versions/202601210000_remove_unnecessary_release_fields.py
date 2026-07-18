"""remove_unnecessary_release_fields

Revision ID: 202601210000
Revises: 202601190000
Create Date: 2026-01-21 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202601210000"
down_revision: str | None = "202601190000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Remove unnecessary fields from releases table
    # These fields are redundant or should be on other tables:
    # - tmdb_id: redundant, exists on Movie
    # - title_cover_url: should be on Disc, not Release
    # - info_title: should be on Disc, not Release
    op.drop_column("releases", "tmdb_id")
    op.drop_column("releases", "title_cover_url")
    op.drop_column("releases", "info_title")


def downgrade() -> None:
    # Re-add the columns (nullable since we're losing data)
    op.add_column("releases", sa.Column("tmdb_id", sa.String(), nullable=True))
    op.add_column("releases", sa.Column("title_cover_url", sa.Text(), nullable=True))
    op.add_column("releases", sa.Column("info_title", sa.Text(), nullable=True))
