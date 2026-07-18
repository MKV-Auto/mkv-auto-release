"""add_title_seq_to_disc_titles

Revision ID: 202601180000
Revises: 202601170000
Create Date: 2026-01-18 00:00:00.000000
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "202601180000"
down_revision: str | None = "202601170000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "disc_titles",
        sa.Column("title_seq", sa.Integer(), nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_column("disc_titles", "title_seq")
