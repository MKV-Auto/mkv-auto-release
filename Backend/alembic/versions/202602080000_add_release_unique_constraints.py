"""add unique constraints to prevent duplicate releases

Revision ID: 202602080000
Revises: 202601271000
Create Date: 2026-02-08 00:00:00.000000

Adds unique constraints to prevent duplicate releases:
- UNIQUE(movie_id, boxset_id) WHERE boxset_id IS NOT NULL (releases in boxsets)
- UNIQUE(movie_id) WHERE boxset_id IS NULL (standalone releases)

This prevents race conditions where multiple concurrent requests could create
duplicate releases for the same movie+boxset combination.
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from core.logging_utils import get_logger

logger = get_logger(__name__)

revision: str = "202602080000"
down_revision: str | None = "202601271000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Before adding unique constraints, identify and handle existing duplicates
    # For releases in boxsets, keep the one with the most complete data (most recent updated_at)
    # For standalone releases, keep the one with most discs or most recent
    
    connection = op.get_bind()
    
    # Find duplicate releases in boxsets (same movie_id + boxset_id)
    duplicates_in_boxsets = connection.execute(sa.text("""
        SELECT movie_id, boxset_id, array_agg(id ORDER BY updated_at DESC) as release_ids
        FROM releases
        WHERE boxset_id IS NOT NULL
        GROUP BY movie_id, boxset_id
        HAVING COUNT(*) > 1
    """)).fetchall()
    
    # For each group of duplicates, keep the first (most recent), delete others
    for movie_id, boxset_id, release_ids in duplicates_in_boxsets:
        # Keep first (most recent), delete rest
        keep_id = release_ids[0]
        delete_ids = release_ids[1:]
        
        logger.info("Found %s duplicate releases for movie %s in boxset %s; keeping %s, deleting %s", len(release_ids), movie_id, boxset_id, keep_id, delete_ids)
        
        # Move discs from deleted releases to kept release
        for delete_id in delete_ids:
            connection.execute(sa.text("""
                UPDATE discs 
                SET release_id = :keep_id 
                WHERE release_id = :delete_id
            """), {"keep_id": keep_id, "delete_id": delete_id})
            
            # Delete the duplicate release
            connection.execute(sa.text("""
                DELETE FROM releases WHERE id = :delete_id
            """), {"delete_id": delete_id})
    
    # Find duplicate standalone releases (same movie_id, boxset_id IS NULL)
    duplicates_standalone = connection.execute(sa.text("""
        SELECT movie_id, array_agg(id ORDER BY updated_at DESC) as release_ids
        FROM releases
        WHERE boxset_id IS NULL
        GROUP BY movie_id
        HAVING COUNT(*) > 1
    """)).fetchall()
    
    # For each group of standalone duplicates, keep the first, delete others
    for movie_id, release_ids in duplicates_standalone:
        keep_id = release_ids[0]
        delete_ids = release_ids[1:]
        
        logger.info("Found %s duplicate standalone releases for movie %s; keeping %s, deleting %s", len(release_ids), movie_id, keep_id, delete_ids)
        
        # Move discs from deleted releases to kept release
        for delete_id in delete_ids:
            connection.execute(sa.text("""
                UPDATE discs 
                SET release_id = :keep_id 
                WHERE release_id = :delete_id
            """), {"keep_id": keep_id, "delete_id": delete_id})
            
            # Delete the duplicate release
            connection.execute(sa.text("""
                DELETE FROM releases WHERE id = :delete_id
            """), {"delete_id": delete_id})
    
    # Now add the unique constraints
    # Add partial unique constraint for releases in boxsets
    # Only one release per movie+boxset combination
    op.create_index(
        "uq_releases_movie_boxset",
        "releases",
        ["movie_id", "boxset_id"],
        unique=True,
        postgresql_where=sa.text("boxset_id IS NOT NULL")
    )
    
    # Add partial unique constraint for standalone releases
    # Only one standalone release per movie
    op.create_index(
        "uq_releases_movie_standalone",
        "releases",
        ["movie_id"],
        unique=True,
        postgresql_where=sa.text("boxset_id IS NULL")
    )


def downgrade() -> None:
    # Drop unique constraints
    op.drop_index("uq_releases_movie_standalone", table_name="releases")
    op.drop_index("uq_releases_movie_boxset", table_name="releases")
