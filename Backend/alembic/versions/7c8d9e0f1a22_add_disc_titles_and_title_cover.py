"""Add disc_titles table and title cover url on releases

Revision ID: 7c8d9e0f1a22
Revises: 1f2d3c4b5a60
Create Date: 2025-03-22 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "7c8d9e0f1a22"
down_revision: str | None = "1f2d3c4b5a60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("title_cover_url", sa.Text(), nullable=True))
    op.create_table(
        "disc_titles",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("disc_id", sa.String(), sa.ForeignKey("discs.id"), nullable=False),
        sa.Column("track_id", sa.String(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("source_file", sa.String(), nullable=True),
        sa.Column("segment_map", sa.String(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("duration_raw", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("display_size", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("type", sa.String(), nullable=True),
        sa.Column("season", sa.Integer(), nullable=True),
        sa.Column("episode", sa.Integer(), nullable=True),
        sa.Column("chapters", sa.JSON(), nullable=True),
        sa.Column("streams", sa.JSON(), nullable=True),
        sa.Column("content", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.UniqueConstraint("disc_id", "track_id", name="uq_disc_titles_disc_trackid"),
    )


def downgrade() -> None:
    op.drop_table("disc_titles")
    op.drop_column("releases", "title_cover_url")
