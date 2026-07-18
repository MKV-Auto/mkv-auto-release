"""Add obfuscation_flag and playitem_durations_s to disc_titles

Revision ID: 202605030001
Revises: 202604130700
Create Date: 2026-05-03

Phase 1 of the selective-rip + segment-reorder workstream:
- disc_titles.obfuscation_flag (boolean, default false): captured from
  MakeMKV's MSG:3307 flag bit 0x01000000 at scan time. Detects playlist
  obfuscation mass on Lions Gate-class discs (e.g. Midway 2019).
- disc_titles.playitem_durations_s (JSON, nullable): per-PlayItem durations
  in seconds, parsed from the disc's MPLS files at scan time. Required by
  Phase 2 segment-reorder previews because PlayItem boundaries cannot be
  reliably detected from the joined .mkv post-rip.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605030001'
down_revision = '202604130700'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'obfuscation_flag',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ))
        batch_op.add_column(sa.Column(
            'playitem_durations_s',
            sa.JSON(),
            nullable=True,
        ))


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.drop_column('playitem_durations_s')
        batch_op.drop_column('obfuscation_flag')
