"""add film table and film_id to releases

Revision ID: 202501200000
Revises: 202512171200
Create Date: 2025-01-20 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import text

# revision identifiers, used by Alembic.
revision = "202501200000"
down_revision = "202512171200"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Create films table
    op.create_table(
        "films",
        sa.Column("id", sa.String(), primary_key=True, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("production_year", sa.Integer(), nullable=True),
        sa.Column("tmdb_id", sa.String(), nullable=True, unique=True),
        sa.Column("tmdb_type", sa.String(), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("cover_path", sa.Text(), nullable=True),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=text("now()")),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=text("now()"), onupdate=text("now()")),
    )

    # Add film_id column to releases (nullable initially for migration)
    op.add_column("releases", sa.Column("film_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_releases_film_id", "releases", "films", ["film_id"], ["id"])

    # Backfill: Create films from existing releases
    connection = op.get_bind()
    
    # Get all releases
    releases = connection.execute(text("SELECT id, name, production_year, tmdb_id, type FROM releases")).fetchall()
    
    # Track created films by tmdb_id and by name+year
    films_by_tmdb = {}
    films_by_name_year = {}
    film_id_map = {}  # release_id -> film_id
    
    for release in releases:
        release_id, name, prod_year, tmdb_id, rel_type = release
        
        # Find or create film
        film_id = None
        
        # First try to find by tmdb_id
        if tmdb_id:
            if tmdb_id in films_by_tmdb:
                film_id = films_by_tmdb[tmdb_id]
            else:
                # Check if film with this tmdb_id already exists in DB
                existing = connection.execute(
                    text("SELECT id FROM films WHERE tmdb_id = :tmdb_id"),
                    {"tmdb_id": tmdb_id}
                ).fetchone()
                if existing:
                    film_id = existing[0]
                    films_by_tmdb[tmdb_id] = film_id
        
        # If not found by tmdb_id, try by name+year
        if not film_id and name and prod_year:
            key = (name.lower().strip(), prod_year)
            if key in films_by_name_year:
                film_id = films_by_name_year[key]
            else:
                # Check if film with this name+year already exists
                existing = connection.execute(
                    text("SELECT id FROM films WHERE LOWER(name) = :name AND production_year = :year"),
                    {"name": name.lower().strip(), "year": prod_year}
                ).fetchone()
                if existing:
                    film_id = existing[0]
                    films_by_name_year[key] = film_id
        
        # Create new film if not found
        if not film_id:
            import uuid
            film_id = str(uuid.uuid4())
            film_name = name or "Unknown Film"
            tmdb_type = "tv" if rel_type and rel_type.lower() == "series" else "movie"
            
            connection.execute(
                text("""
                    INSERT INTO films (id, name, production_year, tmdb_id, tmdb_type, created_at, updated_at)
                    VALUES (:id, :name, :prod_year, :tmdb_id, :tmdb_type, now(), now())
                """),
                {
                    "id": film_id,
                    "name": film_name,
                    "prod_year": prod_year,
                    "tmdb_id": tmdb_id,
                    "tmdb_type": tmdb_type,
                }
            )
            
            if tmdb_id:
                films_by_tmdb[tmdb_id] = film_id
            if name and prod_year:
                films_by_name_year[(name.lower().strip(), prod_year)] = film_id
        
        film_id_map[release_id] = film_id
    
    # Update releases with film_id
    for release_id, film_id in film_id_map.items():
        connection.execute(
            text("UPDATE releases SET film_id = :film_id WHERE id = :release_id"),
            {"film_id": film_id, "release_id": release_id}
        )
    
    # Make film_id NOT NULL
    op.alter_column("releases", "film_id", nullable=False)


def downgrade() -> None:
    op.drop_constraint("fk_releases_film_id", "releases", type_="foreignkey")
    op.drop_column("releases", "film_id")
    op.drop_table("films")












