"""Add multi-part episode layout columns to disc_titles

Revision ID: 202608060001
Revises: 202608030002
Create Date: 2026-08-06

A disc's physical layout does not always match one-file-per-episode, and
until now the schema could only express that it did — `episode` is a single
nullable Integer. Both divergences occur in the wild (#796):

  part / part_of    this file is part N of M of ONE episode. Star Wars
                    Rebels S3 D1 splits the premiere "Steps Into Shadow"
                    across two files while TMDB lists one S03E01, so both
                    title rows carried season=3 episode=1 and rendered the
                    same filename — and postprocess silently dropped the
                    second file.

  episode_end       this ONE file covers `episode`..`episode_end`. The
                    inverse case, e.g. a disc that concatenates a
                    two-parter TMDB numbers as E20 and E21.

Each column carries the user_/auto_ split established in 202608030001, so
the TMDB two-parter detector writes `auto_*` and can never overwrite a
hand-correction — `resolved = user ?? auto`, via
`api.crud.set_title_field`.

Expand-only: adds nullable columns, no backfill. Existing rows read NULL,
which means "single episode" — the behaviour they already have — so there
is nothing to migrate and no guarded transform is needed.
"""
from alembic import op
import sqlalchemy as sa


revision = '202608060001'
down_revision = '202608030002'
branch_labels = None
depends_on = None


# (resolved, auto, user) triples — same shape as the season/episode columns
# they sit beside.
_COLUMNS = (
    'part', 'auto_part', 'user_part',
    'part_of', 'auto_part_of', 'user_part_of',
    'episode_end', 'auto_episode_end', 'user_episode_end',
)


def upgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        for name in _COLUMNS:
            batch_op.add_column(sa.Column(name, sa.Integer(), nullable=True))


def downgrade():
    with op.batch_alter_table('disc_titles', schema=None) as batch_op:
        for name in reversed(_COLUMNS):
            batch_op.drop_column(name)
