"""fix release disc cascade to prevent data loss

Revision ID: 202602080001
Revises: 202602080000
Create Date: 2026-02-08 00:01:00.000000

CRITICAL BUG FIX: Prevents cascade deletion of discs when a release is deleted.
Previously, the Release model had cascade="all" which caused deleting a release
to delete ALL associated discs, titles, and data. This migration ensures that
at the database level, deleting a release will SET NULL on disc.release_id
instead of deleting the disc.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from core.logging_utils import get_logger

logger = get_logger(__name__)

revision: str = "202602080001"
down_revision: str | None = "202602080000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Drop existing FK constraint on discs.release_id
    # The constraint name might vary, so we need to find it first
    connection = op.get_bind()
    
    # Find the existing FK constraint name
    result = connection.execute(sa.text("""
        SELECT constraint_name
        FROM information_schema.table_constraints
        WHERE table_name = 'discs'
        AND constraint_type = 'FOREIGN KEY'
        AND constraint_name LIKE '%release%'
    """)).fetchone()
    
    if result:
        constraint_name = result[0]
        logger.info("Found FK constraint: %s", constraint_name)
        
        # Drop the existing constraint
        op.drop_constraint(constraint_name, "discs", type_="foreignkey")
    else:
        logger.info("No existing FK constraint found on discs.release_id")
    
    # Create new FK constraint with ON DELETE SET NULL
    op.create_foreign_key(
        "fk_discs_release_id",
        "discs",
        "releases",
        ["release_id"],
        ["id"],
        ondelete="SET NULL"
    )
    
    logger.info("Fixed FK constraint: discs.release_id now uses ON DELETE SET NULL; deleting a release will no longer cascade-delete its discs")


def downgrade() -> None:
    # Drop the new constraint
    op.drop_constraint("fk_discs_release_id", "discs", type_="foreignkey")
    
    # Recreate the old constraint without ondelete (which defaults to RESTRICT or NO ACTION)
    op.create_foreign_key(
        "fk_discs_release_id",
        "discs",
        "releases",
        ["release_id"],
        ["id"]
    )
