"""merge dev-mode and prior head

Revision ID: f1e2d3c4b5a6
Revises: a3c1f7b2d4e5, dc7f1e2a4b90
Create Date: 2025-02-05
"""

from alembic import op
import sqlalchemy as sa  # noqa: F401

# revision identifiers, used by Alembic.
revision = "f1e2d3c4b5a6"
down_revision = ("a3c1f7b2d4e5", "dc7f1e2a4b90")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
