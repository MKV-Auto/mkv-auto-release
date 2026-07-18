"""Rename track_id columns to title_id

Revision ID: f4c2b3e1c0de
Revises: 1e2c5c0c3f11
Create Date: 2025-03-09 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "f4c2b3e1c0de"
down_revision = "1e2c5c0c3f11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # disc_tracks: drop old constraint, rename column, add new constraint
    op.drop_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", type_="unique")
    op.alter_column("disc_tracks", "track_id", new_column_name="title_id")
    op.create_unique_constraint("uq_disc_tracks_disc_titleid", "disc_tracks", ["disc_id", "title_id"])

    # disc_titles: drop old constraint, rename column, add new constraint
    op.drop_constraint("uq_disc_titles_disc_trackid", "disc_titles", type_="unique")
    op.alter_column("disc_titles", "track_id", new_column_name="title_id")
    op.create_unique_constraint("uq_disc_titles_disc_titleid", "disc_titles", ["disc_id", "title_id"])


def downgrade() -> None:
    # Reverse disc_titles changes
    op.drop_constraint("uq_disc_titles_disc_titleid", "disc_titles", type_="unique")
    op.alter_column("disc_titles", "title_id", new_column_name="track_id")
    op.create_unique_constraint("uq_disc_titles_disc_trackid", "disc_titles", ["disc_id", "track_id"])

    # Reverse disc_tracks changes
    op.drop_constraint("uq_disc_tracks_disc_titleid", "disc_tracks", type_="unique")
    op.alter_column("disc_tracks", "title_id", new_column_name="track_id")
    op.create_unique_constraint("uq_disc_tracks_disc_trackid", "disc_tracks", ["disc_id", "track_id"])
