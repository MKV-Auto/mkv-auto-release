"""Unit tests for release duplicate prevention (race conditions and unique constraints)."""
from __future__ import annotations

import pytest
import threading
from sqlalchemy.exc import IntegrityError

from api import models, crud


# Helper to check if we're using PostgreSQL (SQLite doesn't support partial unique indexes)
def _is_postgresql(session):
    """Check if session is using PostgreSQL."""
    return session.bind.dialect.name == "postgresql"


def _make_movie(session, name: str = "Test Movie") -> models.Movie:
    """Helper to create a test movie."""
    movie = models.Movie(name=name, tmdb_id=12345)
    session.add(movie)
    session.flush()
    return movie


def _make_boxset(session, name: str = "Test Boxset") -> models.Boxset:
    """Helper to create a test boxset."""
    boxset = models.Boxset(
        slug="test-boxset",
        name=name,
        year=2020,
        upc="123456789012",
        cover_front_url="https://example.com/front.jpg"
    )
    session.add(boxset)
    session.flush()
    return boxset


class TestReleaseUniqueConstraints:
    """Test unique constraints prevent duplicate releases."""
    
    def test_cannot_create_duplicate_release_in_boxset(self, test_db):
        """Test that creating two releases with same movie_id + boxset_id fails.
        
        Note: This test requires PostgreSQL with the unique constraints migration applied.
        It will be skipped for SQLite test databases.
        """
        with test_db() as session:
            if not _is_postgresql(session):
                pytest.skip("Unique constraint tests require PostgreSQL")
            
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            # Create first release
            release1 = models.Release(
                slug="test-release",
                type="movie",
                name="Test Release",
                movie_id=movie.id,
                boxset_id=boxset.id,
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release1)
            session.commit()
            
            # Attempt to create duplicate release with same movie_id + boxset_id
            release2 = models.Release(
                slug="test-release-2",  # Different slug
                type="movie",
                name="Test Release 2",  # Different name
                movie_id=movie.id,  # Same movie_id
                boxset_id=boxset.id,  # Same boxset_id
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release2)
            
            # Should raise IntegrityError due to unique constraint
            with pytest.raises(IntegrityError):
                session.commit()
    
    def test_cannot_create_duplicate_standalone_release(self, test_db):
        """Test that creating two standalone releases with same movie_id fails.
        
        Note: This test requires PostgreSQL with the unique constraints migration applied.
        It will be skipped for SQLite test databases.
        """
        with test_db() as session:
            if not _is_postgresql(session):
                pytest.skip("Unique constraint tests require PostgreSQL")
            
            movie = _make_movie(session)
            
            # Create first standalone release
            release1 = models.Release(
                slug="test-release",
                type="movie",
                name="Test Release",
                movie_id=movie.id,
                boxset_id=None,
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release1)
            session.commit()
            
            # Attempt to create duplicate standalone release with same movie_id
            release2 = models.Release(
                slug="test-release-2",  # Different slug
                type="movie",
                name="Test Release 2",  # Different name
                movie_id=movie.id,  # Same movie_id
                boxset_id=None,  # Same (no boxset)
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release2)
            
            # Should raise IntegrityError due to unique constraint
            with pytest.raises(IntegrityError):
                session.commit()
    
    def test_can_create_same_movie_in_different_boxsets(self, test_db):
        """Test that same movie can have releases in different boxsets."""
        with test_db() as session:
            movie = _make_movie(session)
            boxset1 = _make_boxset(session, "Boxset 1")
            boxset2 = _make_boxset(session, "Boxset 2")
            
            # Create release in first boxset
            release1 = models.Release(
                slug="test-release-1",
                type="movie",
                name="Test Release 1",
                movie_id=movie.id,
                boxset_id=boxset1.id,
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release1)
            session.commit()
            
            # Create release in second boxset (should succeed)
            release2 = models.Release(
                slug="test-release-2",
                type="movie",
                name="Test Release 2",
                movie_id=movie.id,
                boxset_id=boxset2.id,  # Different boxset
                release_year=2020,
                upc="123456789012",
                cover_front_url="https://example.com/front.jpg"
            )
            session.add(release2)
            session.commit()
            
            # Both releases should exist
            assert release1.id != release2.id
            assert release1.boxset_id == boxset1.id
            assert release2.boxset_id == boxset2.id


class TestGetOrCreateReleaseIntegrityHandling:
    """Test get_or_create_release handles race conditions gracefully."""
    
    def test_get_or_create_returns_existing_release_in_boxset(self, test_db):
        """Test that get_or_create_release reuses existing release."""
        with test_db() as session:
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            payload = {
                "movie_id": movie.id,
                "boxset_id": boxset.id,
                "group_type": "movie",
                "release_name": "Test Release",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            
            # Create first release
            release1 = crud.get_or_create_release(session, payload)
            session.commit()
            
            # Call again with same payload - should return same release
            release2 = crud.get_or_create_release(session, payload)
            
            assert release1.id == release2.id
            assert release1.movie_id == movie.id
            assert release1.boxset_id == boxset.id

    def test_get_or_create_new_boxset_release_fills_from_boxset_when_payload_minimal(self, test_db):
        """POST with only movie_id + boxset_id must copy edition fields from boxset onto the release."""
        with test_db() as session:
            movie = _make_movie(session, name="Dune: Part Two")
            boxset = models.Boxset(
                slug="dune-2-film-collection",
                name="Dune 2-Film Collection",
                year=2022,
                upc="883929609673",
                asin="B0TESTBOXSET1",
                cover_front_url="https://example.com/boxset-front.jpg",
                cover_back_url="https://example.com/boxset-back.jpg",
            )
            session.add(boxset)
            session.flush()
            payload = {"movie_id": movie.id, "boxset_id": boxset.id, "group_type": "movie"}
            rel = crud.get_or_create_release(session, payload)
            session.commit()
            assert rel is not None
            assert rel.boxset_id == boxset.id
            assert rel.movie_id == movie.id
            assert rel.name == "Dune 2-Film Collection"
            assert rel.release_year == 2022
            assert rel.upc == "883929609673"
            assert rel.asin == "B0TESTBOXSET1"
            assert rel.cover_front_url == "https://example.com/boxset-front.jpg"
            assert rel.cover_back_url == "https://example.com/boxset-back.jpg"
            assert rel.slug == boxset.slug

    def test_get_or_create_returns_existing_standalone_release(self, test_db):
        """Test that get_or_create_release reuses existing standalone release."""
        with test_db() as session:
            movie = _make_movie(session)
            
            payload = {
                "movie_id": movie.id,
                "group_type": "movie",
                "release_name": "Test Release",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            
            # Create first release
            release1 = crud.get_or_create_release(session, payload)
            session.commit()
            
            # Call again with same payload - should return same release
            release2 = crud.get_or_create_release(session, payload)
            
            assert release1.id == release2.id
            assert release1.movie_id == movie.id
            assert release1.boxset_id is None

    def test_standalone_same_movie_different_upc_creates_two_releases(self, test_db):
        """Do not merge a new edition into an arbitrary existing row for the same movie."""
        with test_db() as session:
            movie = _make_movie(session)
            a = {
                "movie_id": movie.id,
                "group_type": "movie",
                "release_name": "Edition A",
                "release_year": 2017,
                "upc": "883929609673",
                "cover_front_url": "https://example.com/a.jpg",
            }
            b = {
                "movie_id": movie.id,
                "group_type": "movie",
                "release_name": "Edition B",
                "release_year": 2022,
                "upc": "5901234123457",
                "cover_front_url": "https://example.com/b.jpg",
            }
            ra = crud.get_or_create_release(session, a)
            session.commit()
            rb = crud.get_or_create_release(session, b)
            session.commit()
            assert ra.id != rb.id
            assert ra.upc == "883929609673"
            assert ra.release_year == 2017
            assert rb.upc == "5901234123457"
            assert rb.release_year == 2022
    
    def test_get_or_create_updates_existing_release_metadata(self, test_db):
        """Test that get_or_create_release updates existing release with new metadata."""
        with test_db() as session:
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            # Create initial release with minimal data
            payload1 = {
                "movie_id": movie.id,
                "boxset_id": boxset.id,
                "group_type": "movie",
                "release_name": "Test Release",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            release1 = crud.get_or_create_release(session, payload1)
            session.commit()
            
            # Call again with additional metadata
            payload2 = {
                "movie_id": movie.id,
                "boxset_id": boxset.id,
                "group_type": "movie",
                "release_name": "Test Release Updated",
                "release_year": 2020,
                "upc": "123456789012",
                "asin": "B08XYZ",
                "cover_front_url": "https://example.com/front.jpg",
                "cover_back_url": "https://example.com/back.jpg",
                "resolution": "2160p"
            }
            release2 = crud.get_or_create_release(session, payload2)
            
            # Should be same release with updated metadata
            assert release1.id == release2.id
            assert release2.asin == "B08XYZ"
            assert release2.cover_back_url == "https://example.com/back.jpg"
            assert release2.resolution == "2160p"


class TestSlugGenerationConsistency:
    """Test slug generation is consistent and uses boxset slug when linked."""
    
    def test_release_in_boxset_uses_boxset_slug(self, test_db):
        """Test that release in boxset uses boxset.slug, not generated slug."""
        with test_db() as session:
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            payload = {
                "movie_id": movie.id,
                "boxset_id": boxset.id,
                "group_type": "movie",
                "release_name": "Different Release Name",  # Different from boxset name
                "release_year": 2021,  # Different from boxset year
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            
            release = crud.get_or_create_release(session, payload)
            session.commit()
            
            # Should use boxset slug, not generated from release_name/release_year
            assert release.slug == boxset.slug
            assert release.slug == "test-boxset"
    
    def test_standalone_release_generates_slug_from_name_and_year(self, test_db):
        """Test that standalone release generates slug from release_name and release_year."""
        with test_db() as session:
            movie = _make_movie(session)
            
            payload = {
                "movie_id": movie.id,
                "group_type": "movie",
                "release_name": "Test Movie Special Edition",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            
            release = crud.get_or_create_release(session, payload)
            session.commit()
            
            # Should generate slug from release_name and release_year
            assert release.slug == "test_movie_special_edition-2020"
    
    def test_add_release_to_boxset_overwrites_slug(self, test_db):
        """Test that add_release_to_boxset always overwrites release.slug with boxset.slug."""
        with test_db() as session:
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            # Create standalone release first
            payload = {
                "movie_id": movie.id,
                "group_type": "movie",
                "release_name": "Test Release",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            release = crud.get_or_create_release(session, payload)
            session.commit()
            
            original_slug = release.slug
            assert original_slug != boxset.slug
            
            # Link to boxset
            release = crud.add_release_to_boxset(session, boxset, release)
            session.commit()
            
            # Slug should be overwritten with boxset.slug
            assert release.slug == boxset.slug
            assert release.slug != original_slug


class TestRaceConditionSimulation:
    """Simulate race conditions to verify IntegrityError handling."""
    
    def test_concurrent_release_creation_handled_gracefully(self, test_db):
        """Test that concurrent creation attempts are handled gracefully via IntegrityError catch."""
        # Note: This is a simplified test. True concurrent testing would require
        # multiple threads/processes and is better suited for integration tests.
        with test_db() as session:
            movie = _make_movie(session)
            boxset = _make_boxset(session)
            
            payload = {
                "movie_id": movie.id,
                "boxset_id": boxset.id,
                "group_type": "movie",
                "release_name": "Test Release",
                "release_year": 2020,
                "upc": "123456789012",
                "cover_front_url": "https://example.com/front.jpg"
            }
            
            # First call creates release
            release1 = crud.get_or_create_release(session, payload)
            assert release1 is not None
            session.commit()
            
            # Second call should find existing release (not create duplicate)
            release2 = crud.get_or_create_release(session, payload)
            assert release2 is not None
            assert release1.id == release2.id
            
            # Verify only one release exists in database
            count = session.query(models.Release).filter(
                models.Release.movie_id == movie.id,
                models.Release.boxset_id == boxset.id
            ).count()
            assert count == 1
