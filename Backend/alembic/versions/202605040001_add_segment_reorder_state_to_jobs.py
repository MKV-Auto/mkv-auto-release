"""Add segment_reorder_state and rip_set to jobs

Revision ID: 202605040001
Revises: 202605030001
Create Date: 2026-05-04

Phase 2 of the selective-rip + segment-reorder workstream:
- jobs.segment_reorder_state (JSON, nullable): the running state of a Path A
  workflow — exploratory_title_index, persisted PlayItem durations, preview
  paths, partial user order, submitted order, matched playlist index, and
  the workflow stage. Null on any job not running through Path A.
- jobs.rip_set (JSON, nullable): list of MakeMKV title indexes to feed the
  per-title rip loop in core.disc.Disc.rip(). Null on every default-path
  rip (the vast majority); set only by Path A after the canonical playlist
  is resolved.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605040001'
down_revision = '202605030001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('segment_reorder_state', sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column('rip_set', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('jobs', schema=None) as batch_op:
        batch_op.drop_column('rip_set')
        batch_op.drop_column('segment_reorder_state')
