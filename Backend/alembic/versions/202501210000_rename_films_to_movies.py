"""rename films table to movies and film_id to movie_id

Revision ID: 202501210000
Revises: 202501200000
Create Date: 2025-01-21 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "202501210000"
down_revision = "202501200000"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Rename the table from films to movies
    op.rename_table("films", "movies")
    
    # Drop the old foreign key constraint
    op.drop_constraint("fk_releases_film_id", "releases", type_="foreignkey")
    
    # Rename the column from film_id to movie_id
    op.alter_column("releases", "film_id", new_column_name="movie_id")
    
    # Recreate the foreign key constraint with the new column name
    op.create_foreign_key(
        "fk_releases_movie_id",
        "releases",
        "movies",
        ["movie_id"],
        ["id"]
    )


def downgrade() -> None:
    # Drop the new foreign key constraint
    op.drop_constraint("fk_releases_movie_id", "releases", type_="foreignkey")
    
    # Rename the column back from movie_id to film_id
    op.alter_column("releases", "movie_id", new_column_name="film_id")
    
    # Recreate the old foreign key constraint
    op.create_foreign_key(
        "fk_releases_film_id",
        "releases",
        "films",
        ["film_id"],
        ["id"]
    )
    
    # Rename the table back from movies to films
    op.rename_table("movies", "films")












