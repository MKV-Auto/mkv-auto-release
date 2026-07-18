"""Add performance indexes for hot query paths.

Indexes on Job, Release, and Disc tables for columns that are frequently
queried by workflow context endpoints, unfinished-jobs queries, and active-job
lookups. These indexes improve response times for:
- GET /discs/{id}/workflow-context (Job.disc_id + Job.job_status)
- GET /jobs/unfinished/workflow-contexts (Job.rip_state + Job.job_status)
- GET /coordinator/initial-state (Job.disc_id, Job.job_status)
- _load_workflow_options (Release.movie_id, Release.boxset_id)
- get_active_job_for_disc / get_active_job_for_hash (Job.disc_id + status)

Revision ID: 202603180000
Revises: 202603150000
Create Date: 2026-03-18
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202603180000"
down_revision: Union[str, None] = "202603150000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent CREATE INDEX. Any user upgrading from a partial pre-1.0 schema
    # (where one or more of these indexes were created manually or by a sibling
    # branch) would otherwise hit `DuplicateTable: relation "idx_*" already
    # exists` here, abort, and leave alembic stuck at the prior revision — so
    # every later migration (including the multi-drive `drive_by_id_serial`
    # column) silently skips and the running app hits `UndefinedColumn` at
    # runtime. Using `CREATE INDEX IF NOT EXISTS` keeps the migration honest
    # while letting partially-applied schemas heal forward.
    op.execute("CREATE INDEX IF NOT EXISTS idx_jobs_disc_id ON jobs (disc_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_jobs_job_status ON jobs (job_status)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_jobs_rip_state ON jobs (rip_state)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at)")
    # Composite index for the most common query pattern:
    # WHERE disc_id = X AND job_status IN ('pending', 'running', 'validating')
    op.execute("CREATE INDEX IF NOT EXISTS idx_jobs_disc_id_status ON jobs (disc_id, job_status)")

    # Release table indexes
    op.execute("CREATE INDEX IF NOT EXISTS idx_releases_movie_id ON releases (movie_id)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_releases_boxset_id ON releases (boxset_id)")

    # Disc table index
    op.execute("CREATE INDEX IF NOT EXISTS idx_discs_release_id ON discs (release_id)")


def downgrade() -> None:
    op.drop_index("idx_discs_release_id", table_name="discs")
    op.drop_index("idx_releases_boxset_id", table_name="releases")
    op.drop_index("idx_releases_movie_id", table_name="releases")
    op.drop_index("idx_jobs_disc_id_status", table_name="jobs")
    op.drop_index("idx_jobs_created_at", table_name="jobs")
    op.drop_index("idx_jobs_rip_state", table_name="jobs")
    op.drop_index("idx_jobs_job_status", table_name="jobs")
    op.drop_index("idx_jobs_disc_id", table_name="jobs")
