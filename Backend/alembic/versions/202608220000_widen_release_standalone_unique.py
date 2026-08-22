"""allow a movie to hold several standalone releases

Revision ID: 202608220000
Revises: 202608060001
Create Date: 2026-08-22 00:00:00.000000

`uq_releases_movie_standalone` (movie_id WHERE boxset_id IS NULL) permitted
exactly ONE standalone release per movie. A series is a single movies row, so
its seasons are separate releases of the same movie and the second one could
never be inserted. In production the failed INSERT fell into the IntegrityError
recovery path, which re-selected the existing release and stamped the new
edition's UPC/ASIN/year over it, then returned it as if it had been created
(mkv-auto#821). A user lost Season Three's details to the Season Two values
they had just typed.

Widen the key to (movie_id, name, upc). Distinct editions coexist; two creates
carrying the same name AND upc still collide, which is the race protection the
original index in 202602080000 was written for.

COALESCE is load-bearing: Postgres treats NULLs as DISTINCT in a unique index,
so a bare (movie_id, name, upc) would let unlimited NULL-named rows through and
lose that protection entirely.

Safety: this only ever RELAXES the key. Every row satisfying UNIQUE(movie_id)
already satisfies the wider index, so no row can collide, and there is nothing
to dedupe, delete or re-parent -- unlike 202602080000, which had to collapse
duplicates before it could add its constraint.
"""
from __future__ import annotations

from alembic import op

revision: str = "202608220000"
down_revision: str | None = "202608060001"
branch_labels = None
depends_on = None

NEW_INDEX = "uq_releases_movie_edition_standalone"
OLD_INDEX = "uq_releases_movie_standalone"


def upgrade() -> None:
    # IF EXISTS / IF NOT EXISTS so the round-trip gate in
    # test_migration_data_safety.py (downgrade -1 then upgrade head) can re-run
    # this against a database where it has already been applied.
    op.execute(f"DROP INDEX IF EXISTS {OLD_INDEX}")
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {NEW_INDEX}
        ON releases (movie_id, COALESCE(name, ''), COALESCE(upc, ''))
        WHERE boxset_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {NEW_INDEX}")
    # Narrowing back to one release per movie fails if the user has since
    # created a second edition -- the rows the upgrade exists to permit are
    # exactly the rows this cannot accommodate. Same honest caveat as
    # 202603210000_disc_titles_unique_disc_index.py. Restoring the old shape
    # would mean deleting a user's releases, which this deliberately will not do.
    op.execute(
        f"""
        CREATE UNIQUE INDEX IF NOT EXISTS {OLD_INDEX}
        ON releases (movie_id)
        WHERE boxset_id IS NULL
        """
    )
