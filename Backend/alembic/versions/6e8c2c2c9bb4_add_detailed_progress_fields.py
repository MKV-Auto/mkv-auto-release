"""add detailed progress fields to jobs

Revision ID: 6e8c2c2c9bb4
Revises: ecde1059ae60
Create Date: 2025-11-24 21:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '6e8c2c2c9bb4'
down_revision: Union[str, None] = 'ecde1059ae60'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('titles_completed', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('total_titles', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('current_title_progress', sa.Integer(), nullable=True))
    op.add_column('jobs', sa.Column('current_title_id', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('per_title_progress', sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'per_title_progress')
    op.drop_column('jobs', 'current_title_id')
    op.drop_column('jobs', 'current_title_progress')
    op.drop_column('jobs', 'total_titles')
    op.drop_column('jobs', 'titles_completed')
