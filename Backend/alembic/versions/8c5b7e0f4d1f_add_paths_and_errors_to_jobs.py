"""add paths and errors to jobs

Revision ID: 8c5b7e0f4d1f
Revises: 6e8c2c2c9bb4
Create Date: 2025-11-25 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8c5b7e0f4d1f'
down_revision: Union[str, None] = '6e8c2c2c9bb4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('tmp_dir', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('output_dir', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('final_paths', sa.JSON(), nullable=True))
    op.add_column('jobs', sa.Column('error_reason', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'error_reason')
    op.drop_column('jobs', 'final_paths')
    op.drop_column('jobs', 'output_dir')
    op.drop_column('jobs', 'tmp_dir')
