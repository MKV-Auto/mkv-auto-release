"""Drop title_id column from disc_titles (use source_file key).

Revision ID: 202512102255
Revises: 202512102240
Create Date: 2025-12-10 22:55:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "202512102255"
down_revision = "202512102240"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.drop_column("title_id")


def downgrade():
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.add_column(sa.Column("title_id", sa.String(), nullable=True))
