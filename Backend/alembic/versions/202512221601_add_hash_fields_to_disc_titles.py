"""Add hash fields to disc_titles for validation

Revision ID: 202512221601
Revises: 202501220002
Create Date: 2025-12-22 16:01:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '202512221601'
down_revision: Union[str, None] = '202501220002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add source_hash and output_hash columns to disc_titles table
    op.add_column('disc_titles', sa.Column('source_hash', sa.String(), nullable=True))
    op.add_column('disc_titles', sa.Column('output_hash', sa.String(), nullable=True))


def downgrade() -> None:
    # Remove hash columns
    op.drop_column('disc_titles', 'output_hash')
    op.drop_column('disc_titles', 'source_hash')

