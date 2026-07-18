"""Reverse the user_type backfill from 202605230001

Revision ID: 202605240001
Revises: 202605230002
Create Date: 2026-05-24

The previous migration (202605230001) was conservative — it backfilled
`user_type = type` for every existing row, on the theory that legacy
edits should not be silently demoted to automated. In practice that
mis-attributed scan-time MakeMKV defaults (and Path A sibling-ignore
marks, m2ts subsumption, etc.) to "user selected" — making almost
every title render with a misleading "User selected" chip.

The right semantic: legacy `type` came from automated detection unless
proven otherwise. The single observable user-action signal we have on
existing data is Path A's canonical match — `disc_titles.index` ==
some `jobs.segment_reorder_state ->> 'matched_playlist_index'` on a
job for the same disc. Every other legacy row gets reclassified as
auto.

Forward: `auto_type = COALESCE(user_type, auto_type)` then
`user_type = NULL` for non-Path-A-canonical rows.

Backward: re-run the original conservative backfill (set `user_type =
type` where user_type is currently null).
"""
from alembic import op


revision = '202605240001'
down_revision = '202605230002'
branch_labels = None
depends_on = None


def upgrade():
    # First, identify Path A canonical title IDs — these are rows whose
    # `index` matches some job's `segment_reorder_state -> matched_playlist_index`
    # for the same disc. Keep their user_type as-is (these are genuine user
    # actions).
    op.execute("""
        WITH path_a_canonicals AS (
            SELECT DISTINCT t.id
            FROM disc_titles t
            JOIN jobs j ON j.disc_id = t.disc_id
            WHERE j.segment_reorder_state IS NOT NULL
              AND (j.segment_reorder_state ->> 'matched_playlist_index')::int = t.index
        )
        UPDATE disc_titles
        SET
            auto_type = CASE WHEN auto_type IS NULL THEN user_type ELSE auto_type END,
            user_type = NULL
        WHERE id NOT IN (SELECT id FROM path_a_canonicals)
          AND user_type IS NOT NULL
    """)


def downgrade():
    # Restore the conservative backfill — set user_type = type where empty.
    op.execute(
        "UPDATE disc_titles "
        "SET user_type = type "
        "WHERE type IS NOT NULL AND user_type IS NULL"
    )
