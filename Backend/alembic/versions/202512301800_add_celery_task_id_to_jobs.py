"""add_celery_task_id_to_jobs

Revision ID: 202512301800
Revises: 202512230000
Create Date: 2025-12-30 18:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202512301800"
down_revision: str | None = "202512230000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add celery_task_id column to jobs table
    op.add_column("jobs", sa.Column("celery_task_id", sa.String(), nullable=True))
    
    # Create unique constraint to ensure one job per task_id
    op.create_unique_constraint("uq_jobs_celery_task_id", "jobs", ["celery_task_id"])
    
    # Create index for better query performance when looking up jobs by task_id
    op.create_index("idx_jobs_celery_task_id", "jobs", ["celery_task_id"])


def downgrade() -> None:
    # Drop index
    op.drop_index("idx_jobs_celery_task_id", table_name="jobs")
    
    # Drop unique constraint
    op.drop_constraint("uq_jobs_celery_task_id", "jobs", type_="unique")
    
    # Drop column
    op.drop_column("jobs", "celery_task_id")


