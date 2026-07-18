"""Add boxsets and boxset_releases tables

Revision ID: a1b2c3d4e5f6
Revises: dc7f1e2a4b90
Create Date: 2025-12-20 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "202501220001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create boxsets table
    op.create_table(
        "boxsets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("slug", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("sort_title", sa.String(), nullable=True),
        sa.Column("upc", sa.String(), nullable=True),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("year", sa.Integer(), nullable=True),
        sa.Column("locale", sa.String(), nullable=True),
        sa.Column("region_code", sa.String(), nullable=True),
        sa.Column("cover_front_url", sa.Text(), nullable=True),
        sa.Column("cover_back_url", sa.Text(), nullable=True),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("release_date", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finalized", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("finalized_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finalize_result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
    )
    
    # Create boxset_releases junction table
    op.create_table(
        "boxset_releases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("boxset_id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("disc_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["boxset_id"], ["boxsets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("boxset_id", "release_id", name="uq_boxset_releases_boxset_release"),
        sa.UniqueConstraint("release_id", name="uq_boxset_releases_release_id"),
    )
    
    # Create indexes
    op.create_index("idx_boxset_releases_boxset_id", "boxset_releases", ["boxset_id"])
    op.create_index("idx_boxset_releases_release_id", "boxset_releases", ["release_id"])


def downgrade() -> None:
    op.drop_index("idx_boxset_releases_release_id", table_name="boxset_releases")
    op.drop_index("idx_boxset_releases_boxset_id", table_name="boxset_releases")
    op.drop_table("boxset_releases")
    op.drop_table("boxsets")

