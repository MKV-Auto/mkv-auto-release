"""Store makemkv info label on releases and discs

Revision ID: b6f4a7c12d34
Revises: 7c8d9e0f1a22
Create Date: 2025-03-24 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b6f4a7c12d34"
down_revision: str | None = "7c8d9e0f1a22"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("releases", sa.Column("info_label", sa.Text(), nullable=True))
    op.add_column("discs", sa.Column("info_label", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("discs", "info_label")
    op.drop_column("releases", "info_label")
