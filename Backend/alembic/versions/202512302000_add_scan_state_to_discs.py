"""add_scan_state_to_discs

Revision ID: 202512302000
Revises: 202512301800
Create Date: 2025-12-30 20:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "202512302000"
down_revision: str | None = "202512301800"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create enum type for scan_state
    scan_state_enum = postgresql.ENUM('pending', 'scanning', 'completed', 'failed', name='scan_state_enum', create_type=True)
    scan_state_enum.create(op.get_bind(), checkfirst=True)
    
    # Add scan_state column to discs table
    op.add_column("discs", sa.Column("scan_state", scan_state_enum, nullable=True))
    
    # Add scan_attempts column
    op.add_column("discs", sa.Column("scan_attempts", sa.Integer(), nullable=False, server_default=sa.text("0")))
    
    # Add last_scan_error column
    op.add_column("discs", sa.Column("last_scan_error", sa.Text(), nullable=True))
    
    # Add last_scan_at column
    op.add_column("discs", sa.Column("last_scan_at", sa.TIMESTAMP(timezone=True), nullable=True))
    
    # Add info_log_stored column
    op.add_column("discs", sa.Column("info_log_stored", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    
    # Create index on scan_state for faster queries
    op.create_index("idx_discs_scan_state", "discs", ["scan_state"])


def downgrade() -> None:
    # Drop index
    op.drop_index("idx_discs_scan_state", table_name="discs")
    
    # Drop columns
    op.drop_column("discs", "info_log_stored")
    op.drop_column("discs", "last_scan_at")
    op.drop_column("discs", "last_scan_error")
    op.drop_column("discs", "scan_attempts")
    op.drop_column("discs", "scan_state")
    
    # Drop enum type
    op.execute("DROP TYPE IF EXISTS scan_state_enum")

