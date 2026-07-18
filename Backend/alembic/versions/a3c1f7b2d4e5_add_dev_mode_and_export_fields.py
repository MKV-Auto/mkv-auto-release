"""add dev mode and export path to jobs

Revision ID: a3c1f7b2d4e5
Revises: d7e8f9a0b1c3
Create Date: 2025-02-05
"""

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "a3c1f7b2d4e5"
down_revision = "d7e8f9a0b1c3"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("jobs", sa.Column("dev_mode", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("jobs", sa.Column("dev_validation", sa.JSON(), nullable=True))
    op.add_column("jobs", sa.Column("export_path", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("jobs", "export_path")
    op.drop_column("jobs", "dev_validation")
    op.drop_column("jobs", "dev_mode")
