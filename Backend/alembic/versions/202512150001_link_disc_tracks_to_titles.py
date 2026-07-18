"""Link disc tracks to disc titles via title_id FK and drop track_id"""
from __future__ import annotations

from datetime import datetime
import uuid
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.sql import table, column, text

# revision identifiers, used by Alembic.
revision: str = "202512150001"
down_revision: Union[str, None] = "202512120915"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()

    # Backfill title_id from matching disc_title.source_file == disc_track.track_id
    conn.execute(
        text(
            """
            UPDATE disc_tracks dt
            SET title_id = dtit.id
            FROM disc_titles dtit
            WHERE dt.title_id IS NULL
              AND dt.disc_id = dtit.disc_id
              AND dtit.source_file = dt.track_id
            """
        )
    )

    # Create stub titles for any remaining tracks lacking a title_id
    missing = conn.execute(
        text("SELECT id, disc_id, track_id FROM disc_tracks WHERE title_id IS NULL")
    ).fetchall()
    if missing:
        now = datetime.utcnow()
        disc_titles = table(
            "disc_titles",
            column("id", sa.String),
            column("disc_id", sa.String),
            column("index", sa.Integer),
            column("comment", sa.Text),
            column("source_file", sa.String),
            column("created_at", sa.TIMESTAMP(timezone=True)),
            column("updated_at", sa.TIMESTAMP(timezone=True)),
        )
        inserts = []
        updates = []
        for row in missing:
            title_id = str(uuid.uuid4())
            inserts.append(
                {
                    "id": title_id,
                    "disc_id": row.disc_id,
                    "index": None,
                    "comment": None,
                    "source_file": row.track_id,
                    "created_at": now,
                    "updated_at": now,
                }
            )
            updates.append({"track_id": row.id, "title_id": title_id})
        op.bulk_insert(disc_titles, inserts)
        for upd in updates:
            conn.execute(
                text("UPDATE disc_tracks SET title_id = :title_id WHERE id = :track_id"),
                upd,
            )

    # Enforce title_id
    op.alter_column("disc_tracks", "title_id", existing_type=sa.String(), nullable=False)

    # Drop old unique constraint and create a new one keyed on title_id
    op.drop_constraint("uq_disc_tracks_disc_title_stream", "disc_tracks", type_="unique")
    op.create_unique_constraint(
        "uq_disc_tracks_disc_title_stream",
        "disc_tracks",
        ["disc_id", "title_id", "stream_index"],
    )

    # Add FK to disc_titles
    op.create_foreign_key(
        "fk_disc_tracks_title",
        "disc_tracks",
        "disc_titles",
        ["title_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Drop track_id column
    op.drop_column("disc_tracks", "track_id")


def downgrade() -> None:
    # Re-introduce track_id column to restore prior shape (best-effort).
    op.add_column(
        "disc_tracks",
        sa.Column("track_id", sa.String(), nullable=True),
    )
    # Backfill track_id from title_id/source_file
    conn = op.get_bind()
    conn.execute(
        text(
            """
            UPDATE disc_tracks dt
            SET track_id = dtit.source_file
            FROM disc_titles dtit
            WHERE dtit.id = dt.title_id
              AND dt.track_id IS NULL
            """
        )
    )
    op.alter_column("disc_tracks", "track_id", existing_type=sa.String(), nullable=False)

    # Drop FK and unique constraint using title_id
    op.drop_constraint("fk_disc_tracks_title", "disc_tracks", type_="foreignkey")
    op.drop_constraint("uq_disc_tracks_disc_title_stream", "disc_tracks", type_="unique")
    op.create_unique_constraint(
        "uq_disc_tracks_disc_title_stream",
        "disc_tracks",
        ["disc_id", "track_id", "stream_index"],
    )
    op.alter_column("disc_tracks", "title_id", existing_type=sa.String(), nullable=True)
