"""Add rip_timing, contribution, verification columns

Revision ID: 202604130700
Revises: abcd1234droprelslug, 202604081200, 202512120915, 202501220001
Create Date: 2026-04-13

Merges all current heads and adds columns from PR #360:
- jobs: rip_started_at, rip_completed_at
- discs: discdb_contribution_status, discdb_contribution_notes,
         discdb_exported_at, discdb_submitted_at, discdb_verification_status
"""
from alembic import op
import sqlalchemy as sa

revision = '202604130700'
down_revision = ('abcd1234droprelslug', '202604081200', '202512120915', '202501220001')
branch_labels = None
depends_on = None


def upgrade():
    # Jobs table: rip timing (#344)
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('rip_started_at', sa.TIMESTAMP(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('rip_completed_at', sa.TIMESTAMP(timezone=True), nullable=True))

    # Discs table: contribution tracking (#334), verification (#338)
    with op.batch_alter_table('discs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('discdb_contribution_status', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('discdb_contribution_notes', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('discdb_exported_at', sa.TIMESTAMP(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('discdb_submitted_at', sa.TIMESTAMP(timezone=True), nullable=True))
        batch_op.add_column(sa.Column('discdb_verification_status', sa.String(), nullable=True))


def downgrade():
    with op.batch_alter_table('discs', schema=None) as batch_op:
        batch_op.drop_column('discdb_verification_status')
        batch_op.drop_column('discdb_submitted_at')
        batch_op.drop_column('discdb_exported_at')
        batch_op.drop_column('discdb_contribution_notes')
        batch_op.drop_column('discdb_contribution_status')

    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('rip_completed_at')
        batch_op.drop_column('rip_started_at')
