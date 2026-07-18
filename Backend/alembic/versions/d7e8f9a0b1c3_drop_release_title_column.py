"""Drop redundant release.title column and use name only

Revision ID: d7e8f9a0b1c3
Revises: c1d2e3f4a5b6
Create Date: 2025-03-24 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d7e8f9a0b1c3"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Ensure name is populated with prior title if empty, then drop title
    op.execute("UPDATE releases SET name = COALESCE(name, title) WHERE name IS NULL AND title IS NOT NULL")
    op.drop_column("releases", "title")


def downgrade() -> None:
    op.add_column("releases", sa.Column("title", sa.String(), nullable=True))
    op.execute("UPDATE releases SET title = name WHERE title IS NULL AND name IS NOT NULL")
