"""remove unique constraint on boxsets.slug

Revision ID: 202501220003
Revises: 202501220002
Create Date: 2025-01-22 00:00:03.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision: str = "202501220003"
down_revision: Union[str, None] = "202501220002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the unique constraint on boxsets.slug
    # Try both possible names (PostgreSQL constraint name vs index name)
    op.execute("ALTER TABLE boxsets DROP CONSTRAINT IF EXISTS boxsets_slug_key")
    op.execute("DROP INDEX IF EXISTS ix_boxsets_slug")
    # Create a non-unique index for performance
    op.execute("CREATE INDEX IF NOT EXISTS ix_boxsets_slug ON boxsets(slug)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_boxsets_slug")
    # Recreate unique constraint
    op.execute("CREATE UNIQUE INDEX ix_boxsets_slug ON boxsets(slug)")

