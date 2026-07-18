"""Add disc_titles.auto_type and disc_titles.user_type

Revision ID: 202605230001
Revises: 202605190001
Create Date: 2026-05-23

Splits `disc_titles.type` into two source-aware columns so the UI can
distinguish automated detection (DiscDB import, Path A sibling-ignore,
subsumed m2ts marks, MakeMKV decoy flag, scan-time defaults) from
direct user input (PATCH edits, Path A canonical-match selection,
"previous order had decoys" flag).

The existing `type` column stays as the denormalized "effective" cache
— `user_type ?? auto_type` — so legacy reads continue to work without
touching every consumer in the codebase. New writes go through
`api.crud.set_title_type(title, value, source)` which keeps the two
source columns and the cache in sync.

Backfill: every existing row's current `type` value gets copied to
`user_type`. This is the conservative default — it treats legacy edits
as user-initiated, so nothing gets silently "demoted" to automated.
Discs scanned BEFORE this migration won't surface their auto provenance
retroactively, but the new chips on freshly-scanned rows + freshly-
edited rows render correctly from day one.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605230001'
down_revision = '202605190001'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_type', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('user_type', sa.String(), nullable=True))

    # Backfill: legacy `type` values are assumed user-initiated.
    op.execute(
        "UPDATE disc_titles "
        "SET user_type = type "
        "WHERE type IS NOT NULL AND user_type IS NULL"
    )


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.drop_column('user_type')
        batch_op.drop_column('auto_type')
