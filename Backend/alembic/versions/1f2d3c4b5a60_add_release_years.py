"""Add release_year and production_year to releases

Revision ID: 1f2d3c4b5a60
Revises: 9d6f3d7c4b21
Create Date: 2025-03-21 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "1f2d3c4b5a60"
down_revision: str | None = "9d6f3d7c4b21"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("release_year", sa.Integer(), nullable=True))
    op.add_column("releases", sa.Column("production_year", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("releases", "production_year")
    op.drop_column("releases", "release_year")
