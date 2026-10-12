"""Add library_media_files — the video-library catalog for DV7 conversion (#894).

Additive, expand-only: a new table, no existing table or column touched, no
backfill (rows are created by the library scan). Older code never reads it.
Reversible: downgrade drops the table, losing only the catalog, which the
next scan rebuilds from the files on disk.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202610110000"
down_revision: str | None = "202609060000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "library_media_files",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("library_root", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mtime", sa.Float(), nullable=True),
        sa.Column("video_codec", sa.String(), nullable=True),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("dv_profile", sa.Integer(), nullable=True),
        sa.Column("duration", sa.Float(), nullable=True),
        sa.Column("status", sa.String(), server_default=sa.text("'catalogued'"), nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("scanned_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("converted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("path"),
    )
    op.create_index("ix_library_media_files_status", "library_media_files", ["status"])


def downgrade() -> None:
    op.drop_index("ix_library_media_files_status", table_name="library_media_files")
    op.drop_table("library_media_files")
