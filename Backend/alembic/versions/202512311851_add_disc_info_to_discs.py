"""add_disc_info_to_discs

Revision ID: 202512311851
Revises: 202512302000
Create Date: 2025-12-31 18:51:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "202512311851"
down_revision: str | None = "202512302000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add disc_info JSON column to discs table
    op.add_column("discs", sa.Column("disc_info", postgresql.JSON(astext_type=sa.Text()), nullable=True))


def downgrade() -> None:
    # Drop disc_info column
    op.drop_column("discs", "disc_info")



