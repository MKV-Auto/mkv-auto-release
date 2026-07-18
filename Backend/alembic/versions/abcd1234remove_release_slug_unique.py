"""drop unique constraint on releases.slug

Revision ID: abcd1234droprelslug
Revises: f1e2d3c4b5a6
Create Date: 2025-02-05
"""

from alembic import op
import sqlalchemy as sa  # noqa: F401


# revision identifiers, used by Alembic.
revision = "abcd1234droprelslug"
down_revision = "f1e2d3c4b5a6"
branch_labels = None
depends_on = None


def upgrade():
    # Drop the unique index (PostgreSQL creates unique indexes for unique constraints)
    # Try both possible names
    op.execute("DROP INDEX IF EXISTS ix_releases_slug")
    # Also try dropping constraint if it exists (PostgreSQL constraint name)
    op.execute("ALTER TABLE releases DROP CONSTRAINT IF EXISTS releases_slug_key")
    # Create a non-unique index for performance
    op.execute("CREATE INDEX IF NOT EXISTS ix_releases_slug ON releases(slug)")


def downgrade():
    op.execute("DROP INDEX IF EXISTS ix_releases_slug")
    # Recreate unique index
    op.execute("CREATE UNIQUE INDEX ix_releases_slug ON releases(slug)")
