"""add workflow_step to jobs

Revision ID: 202601241200
Revises: 202601231000
Create Date: 2026-01-24 12:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision: str = "202601241200"
down_revision: str | None = "202601231000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("workflow_step", sa.String(), nullable=True))
    op.execute(
        """
        UPDATE jobs j
        SET workflow_step = COALESCE(
            j.workflow_step,
            d.label_draft->>'workflow_step',
            j.disc_payload->'label_draft'->>'workflow_step'
        )
        FROM discs d
        WHERE j.disc_id = d.id
          AND j.workflow_step IS NULL
        """
    )


def downgrade() -> None:
    op.drop_column("jobs", "workflow_step")
