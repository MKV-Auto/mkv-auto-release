"""Add file_path, file_path_stage, and active columns to disc_titles.

- file_path: current absolute path to the MKV file on disk
- file_path_stage: which pipeline stage last set file_path ("rip", "postprocess", "transfer")
- active: primary within a duplicate group (True = primary, None/False = secondary)

Revision ID: 202603200000
Revises: 202603180000
Create Date: 2026-03-20
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "202603200000"
down_revision: Union[str, None] = "202603180000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("disc_titles", sa.Column("file_path", sa.Text(), nullable=True))
    op.add_column("disc_titles", sa.Column("file_path_stage", sa.String(), nullable=True))
    op.add_column("disc_titles", sa.Column("active", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("disc_titles", "active")
    op.drop_column("disc_titles", "file_path_stage")
    op.drop_column("disc_titles", "file_path")
