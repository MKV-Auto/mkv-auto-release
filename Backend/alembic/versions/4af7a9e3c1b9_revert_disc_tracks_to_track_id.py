"""Revert disc_tracks to track_id, keep disc_titles on title_id

Revision ID: 4af7a9e3c1b9
Revises: f4c2b3e1c0de
Create Date: 2025-03-09 00:00:00.000000
"""

from alembic import op


# revision identifiers, used by Alembic.
revision = "4af7a9e3c1b9"
down_revision = "f4c2b3e1c0de"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # disc_tracks: back to track_id
    op.drop_constraint("uq_disc_tracks_disc_titleid", "disc_tracks", type_="unique")
    op.alter_column("disc_tracks", "title_id", new_column_name="track_id")
    op.create_unique_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", ["disc_id", "track_id"])
    # disc_titles stay on title_id (no change)


def downgrade() -> None:
    op.drop_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", type_="unique")
    op.alter_column("disc_tracks", "track_id", new_column_name="title_id")
    op.create_unique_constraint("uq_disc_tracks_disc_titleid", "disc_tracks", ["disc_id", "title_id"])
