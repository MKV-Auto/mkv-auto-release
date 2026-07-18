"""Allow null title_id on disc_titles (user-provided only).

Revision ID: 202512102240
Revises: 202512102230
Create Date: 2025-12-10 22:40:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "202512102240"
down_revision = "202512102230"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.alter_column("title_id", existing_type=sa.String(), nullable=True)


def downgrade():
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.alter_column("title_id", existing_type=sa.String(), nullable=False)
