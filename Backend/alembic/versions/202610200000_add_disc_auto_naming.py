"""Add discs.auto_naming — the per-disc "Automatic naming" toggle (#915).

Additive, expand-only: one nullable column, no backfill. NULL means the user
hasn't chosen; the app then infers the toggle from the stored name (on when
it is one MKV-Auto generated, off when it was typed or came from TheDiscDB,
``core.disc_naming.effective_auto_naming``). Reversible: downgrade drops the
column, losing only explicit toggle choices; the inference takes over again.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202610200000"
down_revision: str | None = "202610120000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("discs", sa.Column("auto_naming", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("discs", "auto_naming")
