"""merge_heads

Revision ID: 202601170000
Revises: 202601150000, 3a05d72a753f
Create Date: 2026-01-17 00:00:00.000000
"""

from __future__ import annotations

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "202601170000"
down_revision: tuple[str, str] = ("202601150000", "3a05d72a753f")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
