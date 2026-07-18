"""refactor schema to releases/discs/jobs

Revision ID: 2b4c5d6e7f80
Revises: 1e2c5c0c3f11
Create Date: 2025-03-05 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "2b4c5d6e7f80"
down_revision: Union[str, None] = "1e2c5c0c3f11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop legacy jobs table; downtime/data loss is acceptable for this refactor.
    op.drop_table("jobs")

    op.create_table(
        "releases",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("slug", sa.String(), unique=True, nullable=False),
        sa.Column("type", sa.String(), nullable=False, server_default="movie"),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("tmdb_id", sa.String(), nullable=True),
        sa.Column("upc", sa.String(), nullable=True),
        sa.Column("asin", sa.String(), nullable=True),
        sa.Column("cover_front_url", sa.Text(), nullable=True),
        sa.Column("cover_back_url", sa.Text(), nullable=True),
        sa.Column("finalize_state", sa.String(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
    )

    op.create_table(
        "discs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False, unique=True),
        sa.Column("release_id", sa.String(), sa.ForeignKey("releases.id"), nullable=True),
        sa.Column("disc_number", sa.Integer(), nullable=True),
        sa.Column("disc_slug", sa.String(), nullable=True),
        sa.Column("disc_name", sa.String(), nullable=True),
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("label_payload", sa.JSON(), nullable=True),
        sa.Column("label_draft", sa.JSON(), nullable=True),
        sa.Column("finalize_result", sa.JSON(), nullable=True),
        sa.Column("artifacts", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
    )

    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("disc_id", sa.String(), sa.ForeignKey("discs.id"), nullable=False),
        sa.Column("disc_num", sa.String(), nullable=False),
        sa.Column("mount_point", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default="copy"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("rip_state", sa.String(), nullable=True),
        sa.Column("post_state", sa.String(), nullable=True),
        sa.Column("transfer_state", sa.String(), nullable=True),
        sa.Column("phase", sa.String(), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("logs", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("result_location", sa.Text(), nullable=True),
        sa.Column("tmp_dir", sa.Text(), nullable=True),
        sa.Column("output_dir", sa.Text(), nullable=True),
        sa.Column("final_paths", sa.JSON(), nullable=True),
        sa.Column("error_reason", sa.Text(), nullable=True),
        sa.Column("disc_payload", sa.JSON(), nullable=True),
        sa.Column("titles_completed", sa.Integer(), nullable=True),
        sa.Column("total_titles", sa.Integer(), nullable=True),
        sa.Column("current_title_progress", sa.Integer(), nullable=True),
        sa.Column("current_title_id", sa.String(), nullable=True),
        sa.Column("current_title_number", sa.Integer(), nullable=True),
        sa.Column("per_title_progress", sa.JSON(), nullable=True),
        sa.Column("transfer_status", sa.String(), nullable=True),
        sa.Column("transfer_paths", sa.JSON(), nullable=True),
        sa.Column("transfer_error", sa.Text(), nullable=True),
        sa.Column("transfer_progress", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()"), onupdate=sa.text("now()")),
    )

    op.create_index("ix_discs_content_hash", "discs", ["content_hash"], unique=True)
    op.create_index("ix_jobs_disc_id", "jobs", ["disc_id"])
    op.create_index("ix_releases_slug", "releases", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_jobs_disc_id", table_name="jobs")
    op.drop_index("ix_discs_content_hash", table_name="discs")
    op.drop_index("ix_releases_slug", table_name="releases")
    op.drop_table("jobs")
    op.drop_table("discs")
    op.drop_table("releases")
