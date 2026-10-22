"""Add discs.disc_number_user — a disc number the user set by hand (#873).

Additive, expand-only: one nullable column, no backfill. NULL/False means
the number is assigned in scan order by normalize_disc_numbers_for_release;
True means the user chose it and the normalizer keeps it. Reversible:
downgrade drops the column, and every number goes back to scan order on the
next normalization.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202610200001"
down_revision: str | None = "202610200000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discs", sa.Column("disc_number_user", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("discs", "disc_number_user")
