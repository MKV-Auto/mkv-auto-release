"""drop transfer_status from jobs

Revision ID: 202602150000
Revises: 202602080001
Create Date: 2026-02-15

Unify transfer stage to transfer_state only; backfill from transfer_status then drop column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202602150000"
down_revision: Union[str, None] = "202602080001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Backfill transfer_state from transfer_status where transfer_state is null
    op.execute("""
        UPDATE jobs
        SET transfer_state = transfer_status
        WHERE transfer_state IS NULL AND transfer_status IS NOT NULL
    """)
    op.drop_column("jobs", "transfer_status")


def downgrade() -> None:
    op.add_column("jobs", sa.Column("transfer_status", sa.String(), nullable=True))
    op.execute("""
        UPDATE jobs
        SET transfer_status = transfer_state
        WHERE transfer_status IS NULL AND transfer_state IS NOT NULL
    """)
