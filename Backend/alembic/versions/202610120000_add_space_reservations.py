"""Add space_reservations — disk-space reservations for heavy tasks (#895).

Additive, expand-only: a new table, nothing existing touched, no backfill.
Rows are short-lived (one per running rip/conversion). Reversible: downgrade
drops the table, losing only in-flight reservations, which are re-taken by
the next task start.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202610120000"
down_revision: str | None = "202610110000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "space_reservations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref_id", sa.String(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("fs_key", sa.String(), nullable=False),
        sa.Column("bytes_reserved", sa.BigInteger(), nullable=False),
        sa.Column("written_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "ref_id", name="uq_space_reservations_kind_ref"),
    )
    op.create_index("ix_space_reservations_fs_key", "space_reservations", ["fs_key"])


def downgrade() -> None:
    op.drop_index("ix_space_reservations_fs_key", table_name="space_reservations")
    op.drop_table("space_reservations")
