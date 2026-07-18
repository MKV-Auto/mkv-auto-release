"""remove_unique_constraint_on_releases_slug

Revision ID: 3a05d72a753f
Revises: 202501220004
Create Date: 2026-01-02 20:43:03.051100

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3a05d72a753f'
down_revision: Union[str, None] = '202501220004'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop the unique constraint on releases.slug
    # In PostgreSQL, a unique constraint creates both a constraint and an index
    # Use CASCADE to ensure all dependencies are dropped
    op.execute("DROP INDEX IF EXISTS ix_releases_slug CASCADE")
    op.execute("ALTER TABLE releases DROP CONSTRAINT IF EXISTS releases_slug_key")
    op.execute("ALTER TABLE releases DROP CONSTRAINT IF EXISTS ix_releases_slug")
    # Create a non-unique index for performance
    op.execute("CREATE INDEX IF NOT EXISTS ix_releases_slug ON releases(slug)")


def downgrade() -> None:
    """Downgrade schema."""
    # Recreate unique constraint
    op.execute("DROP INDEX IF EXISTS ix_releases_slug")
    op.execute("CREATE UNIQUE INDEX ix_releases_slug ON releases(slug)")
