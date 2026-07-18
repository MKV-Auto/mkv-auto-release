"""Add per-stream metadata and ordering for disc titles/tracks

Revision ID: b2e1d3c4a567
Revises: 8fb3c4d2a1ef
Create Date: 2025-03-25 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b2e1d3c4a567"
down_revision: str | None = "8fb3c4d2a1ef"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("order_index", sa.Integer(), nullable=True))

    op.add_column("disc_tracks", sa.Column("title_id", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("stream_index", sa.Integer(), server_default=sa.text("0"), nullable=True))
    op.add_column("disc_tracks", sa.Column("stream_type", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("audio_type", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("language_code", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("language", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("codec_short", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("codec_hint", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("name", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("bitrate", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("channels", sa.Integer(), nullable=True))
    op.add_column("disc_tracks", sa.Column("sample_rate", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("bit_depth", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("resolution", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("aspect_ratio", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("reference_frames", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("description", sa.Text(), nullable=True))
    op.add_column("disc_tracks", sa.Column("info", sa.Text(), nullable=True))
    op.add_column("disc_tracks", sa.Column("duration_seconds", sa.Float(), nullable=True))
    op.add_column("disc_tracks", sa.Column("flag", sa.String(), nullable=True))
    op.add_column("disc_tracks", sa.Column("default", sa.Boolean(), nullable=True))
    op.add_column("disc_tracks", sa.Column("layout", sa.String(), nullable=True))

    # Backfill existing rows so new constraints can be applied safely.
    op.execute("UPDATE disc_tracks SET title_id = track_id WHERE title_id IS NULL")
    op.execute("UPDATE disc_tracks SET stream_index = 0 WHERE stream_index IS NULL")

    op.drop_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", type_="unique")
    op.create_unique_constraint(
        "uq_disc_tracks_disc_title_stream",
        "disc_tracks",
        ["disc_id", "track_id", "stream_index"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_disc_tracks_disc_title_stream", "disc_tracks", type_="unique")
    op.create_unique_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", ["disc_id", "track_id"])

    op.drop_column("disc_tracks", "layout")
    op.drop_column("disc_tracks", "default")
    op.drop_column("disc_tracks", "flag")
    op.drop_column("disc_tracks", "duration_seconds")
    op.drop_column("disc_tracks", "info")
    op.drop_column("disc_tracks", "description")
    op.drop_column("disc_tracks", "reference_frames")
    op.drop_column("disc_tracks", "aspect_ratio")
    op.drop_column("disc_tracks", "resolution")
    op.drop_column("disc_tracks", "bit_depth")
    op.drop_column("disc_tracks", "sample_rate")
    op.drop_column("disc_tracks", "channels")
    op.drop_column("disc_tracks", "bitrate")
    op.drop_column("disc_tracks", "name")
    op.drop_column("disc_tracks", "codec_hint")
    op.drop_column("disc_tracks", "codec_short")
    op.drop_column("disc_tracks", "language")
    op.drop_column("disc_tracks", "language_code")
    op.drop_column("disc_tracks", "audio_type")
    op.drop_column("disc_tracks", "stream_type")
    op.drop_column("disc_tracks", "stream_index")
    op.drop_column("disc_tracks", "title_id")

    op.drop_column("disc_titles", "order_index")
