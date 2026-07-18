"""add path_template_schema_version to transfer_configs

Revision ID: 202603150000
Revises: 202603060000
Create Date: 2026-03-15 00:00:00.000000

Path template schema version stored on transfer config when path_template is set
so changes are traceable (issue #129).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "202603150000"
down_revision: Union[str, None] = "202603060000"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "transfer_configs",
        sa.Column("path_template_schema_version", sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("transfer_configs", "path_template_schema_version")
