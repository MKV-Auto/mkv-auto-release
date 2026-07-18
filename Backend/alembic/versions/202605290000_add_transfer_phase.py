"""Add transfer_phase column to jobs.

Revision ID: 202605290000
Revises: 202605240001
Create Date: 2026-05-29

Issue #365 — collapse postprocess into transfer-prep phase.

Adds ``jobs.transfer_phase`` (nullable string) which the unified transfer
worker uses to expose the three sub-phases of the collapsed transfer step:

  * ``"preparing"`` — rename + hash + output validation (what postprocess
    did standalone before the collapse)
  * ``"transferring"`` — actual move/copy to the destination
  * ``"verifying"`` — destination hash + structure validation

The column is nullable. ``NULL`` means "transfer has not begun" (covers
both pre-rip and post-completion idle states; the existing ``transfer_state``
column carries that distinction).

``Job.post_state`` is intentionally **not dropped** in this migration —
it becomes a derived/legacy field that the frontend can still read during
the transition window. A follow-up cleanup migration removes it one
release after the Phase 2 collapse ships. See
``docs/ADR-001-postprocess-collapse.md`` and
``docs/plans/postprocess-collapse-325-365.md`` for the staged rollout.
"""
from alembic import op
import sqlalchemy as sa


revision = '202605290000'
down_revision = '202605240001'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "jobs",
        sa.Column("transfer_phase", sa.String(), nullable=True),
    )


def downgrade():
    op.drop_column("jobs", "transfer_phase")
