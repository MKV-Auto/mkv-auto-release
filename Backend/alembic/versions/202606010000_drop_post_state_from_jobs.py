"""Drop post_state column from jobs.

Revision ID: 202606010000
Revises: 202605300000
Create Date: 2026-06-01

Issue #365 step 5 — final step of the ``post_state`` column drop
workstream. The column has been vestigial since #468 stopped all
non-devmode writes and #469 finished migrating the last reads; all
state queries now go through ``Job.derived_post_state`` (the
hybrid_property added in #461). Frontend continues to read
``post_state`` from API responses, which the backend now serves as
the derived value (#462).

Migration arc: #461 → #462 → #463 → #464 → #465 → #466 → #467 →
#468 → #469 → this migration. 9 PRs.

The downgrade adds the column back as nullable. Operators rolling
back will see ``post_state=NULL`` for all jobs created post-this-
migration (no writers exist to backfill); the application keeps
working because every reader has been migrated to the derivation.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202606010000"
down_revision: str | None = "202605300000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("jobs", "post_state")


def downgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("post_state", sa.String(), nullable=True),
    )
