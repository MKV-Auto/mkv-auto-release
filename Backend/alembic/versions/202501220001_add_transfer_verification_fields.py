"""add transfer verification fields to jobs

Revision ID: 202501220001
Revises: 202501220000
Create Date: 2025-01-22 00:00:01.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '202501220001'
down_revision: Union[str, None] = '202501220000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('jobs', sa.Column('transfer_verification_hash', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_verification_status', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_retry_count', sa.Integer(), nullable=False, server_default=sa.text('0')))
    op.add_column('jobs', sa.Column('transfer_max_retries', sa.Integer(), nullable=False, server_default=sa.text('3')))
    op.add_column('jobs', sa.Column('transfer_speed_mbps', sa.Float(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_bytes_transferred', sa.BigInteger(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_total_bytes', sa.BigInteger(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_conflict_resolution', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_source_cleaned', sa.Boolean(), nullable=False, server_default=sa.text('false')))
    op.add_column('jobs', sa.Column('transfer_validation_status', sa.String(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_validation_error', sa.Text(), nullable=True))
    op.add_column('jobs', sa.Column('transfer_deduplicated', sa.Boolean(), nullable=False, server_default=sa.text('false')))


def downgrade() -> None:
    op.drop_column('jobs', 'transfer_deduplicated')
    op.drop_column('jobs', 'transfer_validation_error')
    op.drop_column('jobs', 'transfer_validation_status')
    op.drop_column('jobs', 'transfer_source_cleaned')
    op.drop_column('jobs', 'transfer_conflict_resolution')
    op.drop_column('jobs', 'transfer_total_bytes')
    op.drop_column('jobs', 'transfer_bytes_transferred')
    op.drop_column('jobs', 'transfer_speed_mbps')
    op.drop_column('jobs', 'transfer_max_retries')
    op.drop_column('jobs', 'transfer_retry_count')
    op.drop_column('jobs', 'transfer_verification_status')
    op.drop_column('jobs', 'transfer_verification_hash')











