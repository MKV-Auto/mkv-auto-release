"""disc_titles: unique (disc_id, source_file); drop unique (disc_id, index)

MakeMKV title index can change across rescans; source_file is the stable key.
Dedupe duplicate (disc_id, source_file) before restoring constraint.

Revision ID: 202604021200
Revises: 202603210000
Create Date: 2026-04-02 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202604021200"
down_revision: str | None = "202603210000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # Duplicate (disc_id, source_file): keep richest row per group.
    conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                    ROW_NUMBER() OVER (
                        PARTITION BY disc_id, source_file
                        ORDER BY
                            (duration IS NOT NULL) DESC,
                            (COALESCE(TRIM(segment_map), '') <> '') DESC,
                            (COALESCE(streams::text, '[]') NOT IN ('[]', 'null')) DESC,
                            COALESCE(mkv_size, 0) DESC,
                            created_at ASC
                    ) AS rn
                FROM disc_titles
                WHERE source_file IS NOT NULL
            ),
            doomed AS (SELECT id FROM ranked WHERE rn > 1)
            DELETE FROM disc_tracks WHERE title_id IN (SELECT id FROM doomed)
            """
        )
    )
    conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                    ROW_NUMBER() OVER (
                        PARTITION BY disc_id, source_file
                        ORDER BY
                            (duration IS NOT NULL) DESC,
                            (COALESCE(TRIM(segment_map), '') <> '') DESC,
                            (COALESCE(streams::text, '[]') NOT IN ('[]', 'null')) DESC,
                            COALESCE(mkv_size, 0) DESC,
                            created_at ASC
                    ) AS rn
                FROM disc_titles
                WHERE source_file IS NOT NULL
            )
            DELETE FROM disc_titles WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
            """
        )
    )

    op.drop_index("uq_disc_titles_disc_index", table_name="disc_titles")
    op.create_unique_constraint(
        "uq_disc_titles_disc_sourcefile",
        "disc_titles",
        ["disc_id", "source_file"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_disc_titles_disc_sourcefile", "disc_titles", type_="unique")
    op.create_index(
        "uq_disc_titles_disc_index",
        "disc_titles",
        ["disc_id", "index"],
        unique=True,
        postgresql_where=sa.text("index IS NOT NULL"),
    )
