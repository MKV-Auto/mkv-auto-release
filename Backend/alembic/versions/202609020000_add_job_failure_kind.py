"""Add jobs.failure_kind — typed failure classification (#853).

Additive, nullable, expand-only: rows written by older code simply have
NULL (rendered as the 'unknown' kind). Reversible.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202609020000"
down_revision: str | None = "202608220000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("failure_kind", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "failure_kind")
