"""add transfer config tables

Revision ID: 202501220000
Revises: 202501210000
Create Date: 2025-01-22 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '202501220000'
down_revision: Union[str, None] = '202501210000'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Create transfer_configs table
    op.create_table(
        'transfer_configs',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('name', sa.String(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('transfer_dir', sa.String(), nullable=True),
        sa.Column('output_dir', sa.String(), nullable=True),
        sa.Column('path_template', sa.String(), nullable=True),
        sa.Column('config_data', sa.JSON(), nullable=True),
        sa.Column('conflict_resolution', sa.String(), nullable=False, server_default='overwrite'),
        sa.Column('cleanup_source', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('enable_deduplication', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('enable_notifications', sa.Boolean(), nullable=False, server_default=sa.text('true')),
        sa.Column('health_check_interval_minutes', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create unique partial index for active config (only one can be active at a time)
    op.execute("""
        CREATE UNIQUE INDEX idx_one_active_config 
        ON transfer_configs (is_active) 
        WHERE is_active = true
    """)
    
    # Create index on is_active for fast lookup
    op.create_index('idx_transfer_configs_is_active', 'transfer_configs', ['is_active'])
    
    # Create transfer_credentials table
    op.create_table(
        'transfer_credentials',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('transfer_config_id', sa.String(), nullable=False),
        sa.Column('type', sa.String(), nullable=False),
        sa.Column('value', sa.Text(), nullable=False),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['transfer_config_id'], ['transfer_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('transfer_config_id', 'type', name='uq_transfer_credentials_config_type')
    )
    
    # Create transfer_history table
    op.create_table(
        'transfer_history',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('job_id', sa.String(), nullable=True),
        sa.Column('transfer_config_id', sa.String(), nullable=True),
        sa.Column('mode', sa.String(), nullable=False),
        sa.Column('source_path', sa.String(), nullable=False),
        sa.Column('destination_path', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('bytes_transferred', sa.BigInteger(), nullable=True),
        sa.Column('transfer_duration_seconds', sa.Float(), nullable=True),
        sa.Column('average_speed_mbps', sa.Float(), nullable=True),
        sa.Column('verification_status', sa.String(), nullable=True),
        sa.Column('verification_hash', sa.String(), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('conflict_resolution', sa.String(), nullable=True),
        sa.Column('source_cleaned', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('was_deduplicated', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('created_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.Column('updated_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['job_id'], ['jobs.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['transfer_config_id'], ['transfer_configs.id'], ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for transfer_history
    op.create_index('idx_transfer_history_job_id', 'transfer_history', ['job_id'])
    op.create_index('idx_transfer_history_config_id', 'transfer_history', ['transfer_config_id'])
    op.create_index('idx_transfer_history_created_at', 'transfer_history', ['created_at'])
    op.create_index('idx_transfer_history_status', 'transfer_history', ['status'])
    
    # Create transfer_health_checks table
    op.create_table(
        'transfer_health_checks',
        sa.Column('id', sa.String(), nullable=False),
        sa.Column('transfer_config_id', sa.String(), nullable=False),
        sa.Column('check_type', sa.String(), nullable=False),
        sa.Column('status', sa.String(), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('response_time_ms', sa.Integer(), nullable=True),
        sa.Column('checked_at', sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text('now()')),
        sa.ForeignKeyConstraint(['transfer_config_id'], ['transfer_configs.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    
    # Create indexes for transfer_health_checks
    op.create_index('idx_transfer_health_config_checked', 'transfer_health_checks', ['transfer_config_id', 'checked_at'])
    op.create_index('idx_transfer_health_status', 'transfer_health_checks', ['status'])


def downgrade() -> None:
    op.drop_index('idx_transfer_health_status', table_name='transfer_health_checks')
    op.drop_index('idx_transfer_health_config_checked', table_name='transfer_health_checks')
    op.drop_table('transfer_health_checks')
    op.drop_index('idx_transfer_history_status', table_name='transfer_history')
    op.drop_index('idx_transfer_history_created_at', table_name='transfer_history')
    op.drop_index('idx_transfer_history_config_id', table_name='transfer_history')
    op.drop_index('idx_transfer_history_job_id', table_name='transfer_history')
    op.drop_table('transfer_history')
    op.drop_table('transfer_credentials')
    op.drop_index('idx_transfer_configs_is_active', table_name='transfer_configs')
    op.execute('DROP INDEX IF EXISTS idx_one_active_config')
    op.drop_table('transfer_configs')











