"""disc_titles: unique (disc_id, index) instead of (disc_id, source_file)

MakeMKV can list the same .m2ts/.mpls for multiple title indices; uniqueness must
follow MakeMKV title index, not the physical source filename.

Revision ID: 202603210000
Revises: 202603200001
Create Date: 2026-03-21 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202603210000"
down_revision: str | None = "202603200001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    conn = op.get_bind()

    # 1) Duplicate (disc_id, source_file): keep richest row per group (unlikely while old
    #    constraint held; covers manual DB edits or restored dumps).
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

    # 2) Duplicate (disc_id, index) where index IS NOT NULL: keep oldest row.
    conn.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT id,
                    ROW_NUMBER() OVER (
                        PARTITION BY disc_id, index ORDER BY created_at ASC
                    ) AS rn,
                    COUNT(*) OVER (PARTITION BY disc_id, index) AS cnt
                FROM disc_titles
                WHERE index IS NOT NULL
            ),
            doomed AS (SELECT id FROM ranked WHERE cnt > 1 AND rn > 1)
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
                        PARTITION BY disc_id, index ORDER BY created_at ASC
                    ) AS rn,
                    COUNT(*) OVER (PARTITION BY disc_id, index) AS cnt
                FROM disc_titles
                WHERE index IS NOT NULL
            )
            DELETE FROM disc_titles WHERE id IN (SELECT id FROM ranked WHERE cnt > 1 AND rn > 1)
            """
        )
    )

    op.drop_constraint("uq_disc_titles_disc_sourcefile", "disc_titles", type_="unique")
    op.create_index(
        "uq_disc_titles_disc_index",
        "disc_titles",
        ["disc_id", "index"],
        unique=True,
        postgresql_where=sa.text("index IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_disc_titles_disc_index", table_name="disc_titles")
    # May fail if duplicate (disc_id, source_file) exist after running upgrade.
    op.create_unique_constraint(
        "uq_disc_titles_disc_sourcefile",
        "disc_titles",
        ["disc_id", "source_file"],
    )
