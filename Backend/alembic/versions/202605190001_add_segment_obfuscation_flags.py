"""Add discs.segment_obfuscation_flags

Revision ID: 202605190001
Revises: 202605170001
Create Date: 2026-05-19

Adds a JSON column `discs.segment_obfuscation_flags` for the per-disc
clip-flag dictionary used by the iterative Path B segment-reorder flow.

Shape: `{ "<clip_id>": "potentially" | "definitely" }`. NULL or {} = no
flags. The segment-reorder matcher consults this dict to exclude mpls
candidates containing `definitely`-flagged clips and to rank-boost
candidates that omit `potentially`-flagged clips.

Per-disc scope is intentional: clip IDs are local to a physical disc;
the flag describes the disc and persists across job restarts AND across
multiple rip attempts on the same disc. Cleared only via an explicit
"clear flags" UI control or when the disc record is deleted.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605190001'
down_revision = '202605170001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('discs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('segment_obfuscation_flags', sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table('discs', schema=None) as batch_op:
        batch_op.drop_column('segment_obfuscation_flags')
