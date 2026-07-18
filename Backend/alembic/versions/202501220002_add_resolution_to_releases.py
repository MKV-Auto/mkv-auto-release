"""add resolution field to releases

Revision ID: 202501220002
Revises: a1b2c3d4e5f6
Create Date: 2025-01-22 00:00:02.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202501220002"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('releases', sa.Column('resolution', sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column('releases', 'resolution')










