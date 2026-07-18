"""Merge branches after track/title split

Revision ID: 8fb3c4d2a1ef
Revises: 4af7a9e3c1b9, d7e8f9a0b1c3
Create Date: 2025-03-09 00:00:00.000000
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "8fb3c4d2a1ef"
down_revision = ("4af7a9e3c1b9", "d7e8f9a0b1c3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No-op merge point
    pass


def downgrade() -> None:
    # No-op merge point
    pass
