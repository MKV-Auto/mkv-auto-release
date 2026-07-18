"""add_disc_size_bytes_to_discs

Revision ID: 202601150000
Revises: 202512311852
Create Date: 2026-01-15 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202601150000"
down_revision: str | None = "202512311852"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discs", sa.Column("disc_size_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("discs", "disc_size_bytes")
