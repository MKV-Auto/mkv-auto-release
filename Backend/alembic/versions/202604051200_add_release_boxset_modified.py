"""Add modified flag to releases and boxsets (DiscDB enrichment tracking)

Revision ID: 202604051200
Revises: 202604041800
Create Date: 2026-04-05 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202604051200"
down_revision: str | None = "202604041800"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column(
        "releases",
        sa.Column("modified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "boxsets",
        sa.Column("modified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("boxsets", "modified")
    op.drop_column("releases", "modified")
