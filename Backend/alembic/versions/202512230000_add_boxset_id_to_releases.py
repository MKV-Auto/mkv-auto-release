"""add_boxset_id_to_releases

Revision ID: 202512230000
Revises: 202512222001
Create Date: 2025-12-23 00:00:00.000000
"""
from __future__ import annotations

import uuid
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "202512230000"
down_revision: str | None = "202512222001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add boxset_id column to releases table
    op.add_column("releases", sa.Column("boxset_id", sa.String(), nullable=True))
    
    # Create foreign key constraint
    op.create_foreign_key(
        "fk_releases_boxset_id",
        "releases",
        "boxsets",
        ["boxset_id"],
        ["id"],
        ondelete="SET NULL"
    )
    
    # Create index for better query performance
    op.create_index("idx_releases_boxset_id", "releases", ["boxset_id"])
    
    # Migrate existing data from boxset_releases junction table
    op.execute("""
        UPDATE releases 
        SET boxset_id = (
            SELECT boxset_id 
            FROM boxset_releases 
            WHERE boxset_releases.release_id = releases.id
            LIMIT 1
        )
        WHERE EXISTS (
            SELECT 1 
            FROM boxset_releases 
            WHERE boxset_releases.release_id = releases.id
        )
    """)
    
    # Drop indexes on boxset_releases table first
    op.drop_index("idx_boxset_releases_release_id", table_name="boxset_releases")
    op.drop_index("idx_boxset_releases_boxset_id", table_name="boxset_releases")
    
    # Drop boxset_releases junction table
    op.drop_table("boxset_releases")


def downgrade() -> None:
    # Recreate boxset_releases junction table
    op.create_table(
        "boxset_releases",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("boxset_id", sa.String(), nullable=False),
        sa.Column("release_id", sa.String(), nullable=False),
        sa.Column("disc_index", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["boxset_id"], ["boxsets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["release_id"], ["releases.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("boxset_id", "release_id", name="uq_boxset_releases_boxset_release"),
        sa.UniqueConstraint("release_id", name="uq_boxset_releases_release_id"),
    )
    
    # Recreate indexes
    op.create_index("idx_boxset_releases_boxset_id", "boxset_releases", ["boxset_id"])
    op.create_index("idx_boxset_releases_release_id", "boxset_releases", ["release_id"])
    
    # Migrate data back from releases.boxset_id to boxset_releases
    # Note: We need to group by boxset_id to calculate disc_index properly
    connection = op.get_bind()
    
    # Get releases grouped by boxset_id
    releases_with_boxset = connection.execute(sa.text("""
        SELECT id, boxset_id, created_at 
        FROM releases 
        WHERE boxset_id IS NOT NULL
        ORDER BY boxset_id, created_at
    """)).fetchall()
    
    # Track disc_index per boxset
    boxset_indices = {}
    for release_id, boxset_id, created_at in releases_with_boxset:
        if boxset_id not in boxset_indices:
            boxset_indices[boxset_id] = 0
        boxset_indices[boxset_id] += 1
        
        connection.execute(sa.text("""
            INSERT INTO boxset_releases (id, boxset_id, release_id, disc_index, created_at)
            VALUES (:id, :boxset_id, :release_id, :disc_index, COALESCE(:created_at, NOW()))
        """), {
            "id": str(uuid.uuid4()),
            "boxset_id": boxset_id,
            "release_id": release_id,
            "disc_index": boxset_indices[boxset_id],
            "created_at": created_at
        })
    
    # Drop index and foreign key from releases
    op.drop_index("idx_releases_boxset_id", table_name="releases")
    op.drop_constraint("fk_releases_boxset_id", "releases", type_="foreignkey")
    
    # Drop boxset_id column from releases
    op.drop_column("releases", "boxset_id")

