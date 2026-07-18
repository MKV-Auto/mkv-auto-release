"""Add job_status, rip_progress, and stage/profile fields.

Revision ID: 202512120915
Revises: 202512102255
Create Date: 2025-12-12 09:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202512120915"
down_revision: Union[str, None] = "202512102255"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("job_status", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("rip_progress", sa.Integer(), nullable=True))
    op.add_column("jobs", sa.Column("label_state", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("finalize_state", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("finalize_release_state", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("stage_profile", sa.String(), nullable=True))
    op.add_column("jobs", sa.Column("discdb_result", sa.String(), nullable=True))
    op.execute("UPDATE jobs SET job_status = status WHERE job_status IS NULL")
    op.execute("UPDATE jobs SET rip_progress = progress WHERE rip_progress IS NULL")

    op.alter_column(
        "jobs",
        "job_status",
        existing_type=sa.String(),
        nullable=False,
        server_default=sa.text("'pending'"),
    )
    op.alter_column(
        "jobs",
        "rip_progress",
        existing_type=sa.Integer(),
        nullable=False,
        server_default=sa.text("0"),
    )

    op.drop_column("jobs", "status")
    op.drop_column("jobs", "progress")


def downgrade() -> None:
    op.add_column("jobs", sa.Column("progress", sa.Integer(), nullable=False, server_default=sa.text("0")))
    op.add_column("jobs", sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'pending'")))

    op.execute("UPDATE jobs SET progress = rip_progress")
    op.execute("UPDATE jobs SET status = job_status")

    op.drop_column("jobs", "discdb_result")
    op.drop_column("jobs", "stage_profile")
    op.drop_column("jobs", "finalize_release_state")
    op.drop_column("jobs", "finalize_state")
    op.drop_column("jobs", "label_state")
    op.drop_column("jobs", "rip_progress")
    op.drop_column("jobs", "job_status")
