"""add transfer_progress

Revision ID: 1e2c5c0c3f11
Revises: 4b7f0f2a9a3a
Create Date: 2025-03-04 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '1e2c5c0c3f11'
down_revision = '4b7f0f2a9a3a'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('transfer_progress', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column('jobs', 'transfer_progress')
