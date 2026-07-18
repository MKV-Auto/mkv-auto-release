"""Add discdb_disc_num to discs (TheDiscDB index reference, not sequencing)

Revision ID: 202604081200
Revises: 202604051200
Create Date: 2026-04-08 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202604081200"
down_revision: str | None = "202604051200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.add_column(
        "discs",
        sa.Column("discdb_disc_num", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_column("discs", "discdb_disc_num")
