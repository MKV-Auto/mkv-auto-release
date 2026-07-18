"""refactor job path fields

Revision ID: 202601230000
Revises: 202601210000
Create Date: 2026-01-23 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "202601230000"
down_revision: str | None = "202601210000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add new columns
    op.add_column("jobs", sa.Column("job_dir", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("ripped_files", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("post_paths", sa.JSON(), nullable=True))
    
    # Remove old columns
    op.drop_column("jobs", "tmp_dir")
    op.drop_column("jobs", "result_location")
    op.drop_column("jobs", "output_dir")
    op.drop_column("jobs", "final_paths")


def downgrade() -> None:
    # Re-add old columns (nullable since we're losing data)
    op.add_column("jobs", sa.Column("tmp_dir", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("result_location", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("output_dir", sa.Text(), nullable=True))
    op.add_column("jobs", sa.Column("final_paths", sa.JSON(), nullable=True))
    
    # Remove new columns
    op.drop_column("jobs", "post_paths")
    op.drop_column("jobs", "ripped_files")
    op.drop_column("jobs", "job_dir")
