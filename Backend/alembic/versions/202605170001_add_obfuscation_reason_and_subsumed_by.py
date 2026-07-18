"""Add disc_titles.obfuscation_reason and disc_titles.subsumed_by_title_id

Revision ID: 202605170001
Revises: 202605040001
Create Date: 2026-05-17

Two new nullable columns on `disc_titles`:

- `obfuscation_reason` (String, nullable): tier-aware decoy/anomaly tag.
  NULL = not flagged. Values used by the app today are
  'segment_set_sibling' (HIGH — non-rep member of a sorted-segment-set
  group), 'path_a_decoy' (HIGH — Path A skipped this row), and
  'makemkv_msg3307' (MEDIUM — MakeMKV's MSG:3307 bit set without group
  context). Backfilled to 'makemkv_msg3307' for every existing row with
  `obfuscation_flag = true` so the boolean and the tier stay consistent
  on upgrade.

- `subsumed_by_title_id` (String, nullable): when an .m2ts clip ID is
  included in another title's segment_map on the same disc, this column
  points to the wrapping (typically .mpls) title's UUID. Used by the
  workflow context to surface "Component clips" in the wrapping title's
  DuplicateGroupPanel and to hide the subsumed row from the default
  title list. No FK constraint — same-disc invariant is enforced at the
  app layer; FKs across this column would force a row-level cascade on
  disc deletion that we don't need.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605170001'
down_revision = '202605040001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('obfuscation_reason', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('subsumed_by_title_id', sa.String(), nullable=True))

    # Backfill: every row currently flagged via MakeMKV's MSG:3307 bit gets
    # the MEDIUM-tier reason so its UI tier matches the boolean.
    op.execute(
        "UPDATE disc_titles "
        "SET obfuscation_reason = 'makemkv_msg3307' "
        "WHERE obfuscation_flag IS TRUE AND obfuscation_reason IS NULL"
    )


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.drop_column('subsumed_by_title_id')
        batch_op.drop_column('obfuscation_reason')
