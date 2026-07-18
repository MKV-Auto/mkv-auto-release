"""Rename disc_tracks to title_streams; drop title-level columns from streams

Revision ID: 202604041800
Revises: 202604021200
Create Date: 2026-04-04 18:00:00.000000

Season, episode, and title type belong on disc_titles only.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202604041800"
down_revision: str | None = "202604021200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.execute(sa.text('ALTER TABLE disc_tracks DROP CONSTRAINT IF EXISTS "fk_disc_tracks_title"'))
    op.execute(sa.text('ALTER TABLE disc_tracks DROP CONSTRAINT IF EXISTS "disc_tracks_disc_id_fkey"'))
    op.drop_constraint("uq_disc_tracks_disc_title_stream", "disc_tracks", type_="unique")

    op.drop_column("disc_tracks", "season")
    op.drop_column("disc_tracks", "episode")
    op.drop_column("disc_tracks", "type")

    op.rename_table("disc_tracks", "title_streams")

    op.create_foreign_key(
        "fk_title_streams_disc_id_fkey",
        "title_streams",
        "discs",
        ["disc_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_title_streams_title_id_fkey",
        "title_streams",
        "disc_titles",
        ["title_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_title_streams_disc_title_stream",
        "title_streams",
        ["disc_id", "title_id", "stream_index"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return

    op.drop_constraint("uq_title_streams_disc_title_stream", "title_streams", type_="unique")
    op.execute(sa.text('ALTER TABLE title_streams DROP CONSTRAINT IF EXISTS "fk_title_streams_title_id_fkey"'))
    op.execute(sa.text('ALTER TABLE title_streams DROP CONSTRAINT IF EXISTS "fk_title_streams_disc_id_fkey"'))

    op.rename_table("title_streams", "disc_tracks")

    op.add_column("disc_tracks", sa.Column("season", sa.Integer(), nullable=True))
    op.add_column("disc_tracks", sa.Column("episode", sa.Integer(), nullable=True))
    op.add_column("disc_tracks", sa.Column("type", sa.String(), nullable=True))

    op.create_foreign_key(
        "disc_tracks_disc_id_fkey",
        "disc_tracks",
        "discs",
        ["disc_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_disc_tracks_title",
        "disc_tracks",
        "disc_titles",
        ["title_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_disc_tracks_disc_title_stream",
        "disc_tracks",
        ["disc_id", "title_id", "stream_index"],
    )
