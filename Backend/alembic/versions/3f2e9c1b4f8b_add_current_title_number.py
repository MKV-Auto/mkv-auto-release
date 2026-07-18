"""add current_title_number

Revision ID: 3f2e9c1b4f8b
Revises: 8c5b7e0f4d1f
Create Date: 2025-11-26 00:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3f2e9c1b4f8b'
down_revision: Union[str, None] = '8c5b7e0f4d1f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('current_title_number', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'current_title_number')
