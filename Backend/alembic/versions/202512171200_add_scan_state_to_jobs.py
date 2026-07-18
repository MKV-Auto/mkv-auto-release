"""add scan_state to jobs"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
# Keep revision IDs short to fit alembic_version varchar(32).
revision = "202512171200"
down_revision = "202512150001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("scan_state", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "scan_state")
