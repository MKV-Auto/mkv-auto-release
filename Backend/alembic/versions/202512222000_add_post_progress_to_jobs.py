"""add_post_progress_to_jobs

Revision ID: 202512222000
Revises: 202512221601
Create Date: 2025-12-22 20:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '202512222000'
down_revision = '202512221601'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('post_progress', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('jobs', 'post_progress')





