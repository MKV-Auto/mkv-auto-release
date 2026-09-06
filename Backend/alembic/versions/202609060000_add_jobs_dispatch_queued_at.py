"""Add jobs.dispatch_queued_at — stage-admission queue marker (#863).

Additive, nullable, expand-only: set when a job is committed to run the
postprocess/transfer pipeline but no concurrency slot is free (the stage
gatekeeper admits jobs FIFO by this timestamp). Rows written by older code
have NULL, which means "not queued" — identical to pre-migration behavior.
Reversible.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202609060000"
down_revision: str | None = "202609020000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dispatch_queued_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "dispatch_queued_at")
