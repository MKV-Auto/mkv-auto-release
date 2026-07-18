"""merge slug removal and hash fields branches

Revision ID: 202501220004
Revises: 202501220003, 202512311852
Create Date: 2025-01-22 00:00:04.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "202501220004"
down_revision: Union[str, tuple[str, ...], None] = ("202501220003", "202512311852")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Merge point - no schema changes needed
    pass


def downgrade() -> None:
    # Merge point - no schema changes needed
    pass

