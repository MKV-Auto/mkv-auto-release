"""add description column to disc_titles

Revision ID: 202512102123
Revises: f4c2b3e1c0de
Create Date: 2025-12-10 21:23:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "202512102123"
down_revision = "abcd1234droprelslug"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("disc_titles", sa.Column("description", sa.Text(), nullable=True))


def downgrade():
    op.drop_column("disc_titles", "description")
