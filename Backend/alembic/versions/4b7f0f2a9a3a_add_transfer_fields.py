"""add transfer fields

Revision ID: 4b7f0f2a9a3a
Revises: 3f2e9c1b4f8b
Create Date: 2025-11-26 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4b7f0f2a9a3a'
down_revision: Union[str, None] = '3f2e9c1b4f8b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('transfer_status', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_paths', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_error', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'transfer_error')
    op.drop_column('jobs', 'transfer_paths')
    op.drop_column('jobs', 'transfer_status')
