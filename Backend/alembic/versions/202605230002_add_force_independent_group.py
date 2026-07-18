"""Add disc_titles.force_independent_group

Revision ID: 202605230002
Revises: 202605230001
Create Date: 2026-05-23

Per-title escape hatch for the dedupe grouping. When set TRUE, the title
is excluded from sorted-segment-set grouping in
`duplicate_info.attach_duplicate_info` so it renders as its own row in
the left rail instead of collapsing into a wrapper or sibling group.

Use case: a disc has two real playlists that legitimately share their
segment set (multi-cut releases with the same scenes but different
audio/sub tracks). The dedupe heuristic groups them; the user wants
to keep them separate. Toggled via the right-editor's "Ungroup"
button (POST /discs/{disc_id}/titles/{title_id}/ungroup-duplicate).
Reversible — same endpoint flips the flag off when called on a
currently-ungrouped title.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605230002'
down_revision = '202605230001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'force_independent_group',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('false'),
        ))


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.drop_column('force_independent_group')
