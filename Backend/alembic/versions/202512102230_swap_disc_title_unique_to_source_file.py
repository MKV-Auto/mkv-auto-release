"""Use source_file for disc_titles uniqueness and backfill values.

Revision ID: 202512102230
Revises: 202512102123
Create Date: 2025-12-10 22:30:00.000000
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "202512102230"
down_revision = "202512102123"
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    conn.execute(
        sa.text(
            """
            UPDATE disc_titles
            SET source_file = COALESCE(source_file, title_id)
            WHERE source_file IS NULL
            """
        )
    )
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.drop_constraint("uq_disc_titles_disc_titleid", type_="unique")
        batch_op.create_unique_constraint("uq_disc_titles_disc_sourcefile", ["disc_id", "source_file"])


def downgrade():
    with op.batch_alter_table("disc_titles") as batch_op:
        batch_op.drop_constraint("uq_disc_titles_disc_sourcefile", type_="unique")
        batch_op.create_unique_constraint("uq_disc_titles_disc_titleid", ["disc_id", "title_id"])
