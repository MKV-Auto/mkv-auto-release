"""make_transfer_progress_not_nullable

Revision ID: 202512222001
Revises: 202512222000
Create Date: 2025-12-22 20:01:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '202512222001'
down_revision = '202512222000'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # First, set all NULL values to 0
    op.execute("UPDATE jobs SET transfer_progress = 0 WHERE transfer_progress IS NULL")
    # Then alter the column to be NOT NULL with default 0
    op.alter_column('jobs', 'transfer_progress',
                    existing_type=sa.Integer(),
                    nullable=False,
                    server_default='0')


def downgrade() -> None:
    # Revert to nullable
    op.alter_column('jobs', 'transfer_progress',
                    existing_type=sa.Integer(),
                    nullable=True,
                    server_default=None)

