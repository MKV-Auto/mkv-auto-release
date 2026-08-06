"""Add user_/auto_ provenance columns for disc_titles label fields

Revision ID: 202608030001
Revises: 202607290000
Create Date: 2026-08-03

Area 1 of the title-state redesign (internal_docs/
BLAST-RADIUS-title-state-v2.md): extend the `type` provenance model
(auto_type/user_type, revision 202605230001) to every user-editable
label field — title, edition, description, season, episode.

The existing resolved columns stay as the denormalized "effective"
cache (`user ?? auto`), so every downstream reader — naming, transfer,
export, serializers — keeps working untouched. New writes go through
`api.crud.set_title_field(title, field, value, source)`, which keeps
the three columns in sync. With provenance in place, automated passes
(DiscDB import/relookup, duplicate-group propagation, detectors) write
the auto columns only and can never overwrite a human's value.

Expand-only: adds nullable columns. The backfill is the SEPARATE next
revision (202608030002) per docs/MIGRATIONS.md — a failed transform
must not strand a half-applied schema change.
"""
from alembic import op
import sqlalchemy as sa


revision = '202608030001'
down_revision = '202607290000'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.add_column(sa.Column('auto_title', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('user_title', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('auto_edition', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('user_edition', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('auto_description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('user_description', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('auto_season', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('user_season', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('auto_episode', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('user_episode', sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        batch_op.drop_column('user_episode')
        batch_op.drop_column('auto_episode')
        batch_op.drop_column('user_season')
        batch_op.drop_column('auto_season')
        batch_op.drop_column('user_description')
        batch_op.drop_column('auto_description')
        batch_op.drop_column('user_edition')
        batch_op.drop_column('auto_edition')
        batch_op.drop_column('user_title')
        batch_op.drop_column('auto_title')
