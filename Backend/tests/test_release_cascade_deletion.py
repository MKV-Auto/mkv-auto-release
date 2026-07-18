"""Tests for release cascade deletion behavior - ensures deleting release doesn't delete discs."""
from __future__ import annotations

import pytest

from api import models, crud


def _make_movie(session, name: str = "Test Movie") -> models.Movie:
    """Helper to create a test movie."""
    movie = models.Movie(name=name, tmdb_id=12345)
    session.add(movie)
    session.flush()
    return movie


def _make_release(session, movie: models.Movie, slug: str = "test-release") -> models.Release:
    """Helper to create a test release."""
    release = models.Release(
        slug=slug,
        type="movie",
        name="Test Release",
        movie_id=movie.id,
        release_year=2020,
        upc="123456789012",
        cover_front_url="https://example.com/front.jpg"
    )
    session.add(release)
    session.flush()
    return release


def _make_disc(session, release: models.Release, content_hash: str = "TESTHASH123") -> models.Disc:
    """Helper to create a test disc."""
    disc = models.Disc(
        content_hash=content_hash,
        release_id=release.id,
        disc_number=1,
        format="Blu-Ray"
    )
    session.add(disc)
    session.flush()
    return disc


def _make_disc_title(session, disc: models.Disc, source_file: str = "title00.mkv") -> models.DiscTitle:
    """Helper to create a test disc title."""
    title = models.DiscTitle(
        disc_id=disc.id,
        index=0,
        source_file=source_file,
        duration=3600.0,
        title="Test Title"
    )
    session.add(title)
    session.flush()
    return title


class TestReleaseCascadeDeletion:
    """Test that deleting a release does NOT cascade-delete its discs."""
    
    def test_deleting_release_does_not_delete_discs(self, test_db):
        """Test that deleting a release leaves discs intact with release_id=NULL."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc = _make_disc(session, release)
            title = _make_disc_title(session, disc)
            
            release_id = release.id
            disc_id = disc.id
            title_id = title.id
            
            session.commit()
            
            # Delete the release directly (not via cleanup function)
            session.delete(release)
            session.commit()
            
            # Disc should still exist with release_id=NULL
            disc_after = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
            assert disc_after is not None, "Disc was incorrectly deleted when release was deleted"
            assert disc_after.release_id is None, "Disc release_id should be NULL after release deletion"
            
            # Title should still exist
            title_after = session.query(models.DiscTitle).filter(models.DiscTitle.id == title_id).first()
            assert title_after is not None, "Title was incorrectly deleted when release was deleted"
    
    def test_deleting_release_with_multiple_discs_leaves_all_discs_intact(self, test_db):
        """Test that deleting a release with multiple discs leaves all discs intact."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc1 = _make_disc(session, release, "HASH1")
            disc2 = _make_disc(session, release, "HASH2")
            disc3 = _make_disc(session, release, "HASH3")
            
            disc_ids = [disc1.id, disc2.id, disc3.id]
            session.commit()
            
            # Delete the release
            session.delete(release)
            session.commit()
            
            # All discs should still exist
            remaining_discs = session.query(models.Disc).filter(
                models.Disc.id.in_(disc_ids)
            ).all()
            
            assert len(remaining_discs) == 3, f"Expected 3 discs, found {len(remaining_discs)}"
            
            # All should have release_id=NULL
            for disc in remaining_discs:
                assert disc.release_id is None, f"Disc {disc.id} release_id should be NULL"


class TestCleanupOrphanedRelease:
    """Test cleanup_orphaned_release() safety checks."""
    
    def test_cleanup_does_not_delete_release_with_discs(self, test_db):
        """Test that cleanup_orphaned_release() does NOT delete release that has discs."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc = _make_disc(session, release)
            
            release_id = release.id
            disc_id = disc.id
            session.commit()
            
            # Attempt cleanup - should NOT delete because release has a disc
            deleted = crud.cleanup_orphaned_release(session, release)
            session.commit()
            
            assert deleted is False, "cleanup_orphaned_release should return False"
            
            # Release should still exist
            release_after = session.query(models.Release).filter(models.Release.id == release_id).first()
            assert release_after is not None, "Release should not be deleted when it has discs"
            
            # Disc should still exist and be linked
            disc_after = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
            assert disc_after is not None, "Disc should still exist"
            assert disc_after.release_id == release_id, "Disc should still be linked to release"
    
    def test_cleanup_deletes_release_with_zero_discs(self, test_db):
        """Test that cleanup_orphaned_release() DOES delete release with zero discs."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            
            release_id = release.id
            session.commit()
            
            # Cleanup should delete because release has no discs
            deleted = crud.cleanup_orphaned_release(session, release)
            session.commit()
            
            assert deleted is True, "cleanup_orphaned_release should return True"
            
            # Release should be deleted
            release_after = session.query(models.Release).filter(models.Release.id == release_id).first()
            assert release_after is None, "Release should be deleted when it has no discs"
    
    def test_cleanup_after_disc_unlink(self, test_db):
        """Test cleanup after unlinking a disc from a release."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc = _make_disc(session, release)
            
            release_id = release.id
            disc_id = disc.id
            session.commit()
            
            # Unlink disc from release
            disc.release_id = None
            session.flush()
            
            # Now cleanup should delete the release
            deleted = crud.cleanup_orphaned_release(session, release)
            session.commit()
            
            assert deleted is True, "cleanup_orphaned_release should delete release after disc unlink"
            
            # Release should be deleted
            release_after = session.query(models.Release).filter(models.Release.id == release_id).first()
            assert release_after is None, "Release should be deleted"
            
            # Disc should still exist
            disc_after = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
            assert disc_after is not None, "Disc should still exist after release cleanup"
            assert disc_after.release_id is None, "Disc should have no release_id"
    
    def test_cleanup_with_multiple_discs_does_not_delete_if_one_remains(self, test_db):
        """Test that cleanup doesn't delete if even one disc remains linked."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc1 = _make_disc(session, release, "HASH1")
            disc2 = _make_disc(session, release, "HASH2")
            
            release_id = release.id
            session.commit()
            
            # Unlink first disc, keep second disc linked
            disc1.release_id = None
            session.flush()
            
            # Cleanup should NOT delete because disc2 is still linked
            deleted = crud.cleanup_orphaned_release(session, release)
            session.commit()
            
            assert deleted is False, "cleanup_orphaned_release should not delete if any disc remains"
            
            # Release should still exist
            release_after = session.query(models.Release).filter(models.Release.id == release_id).first()
            assert release_after is not None, "Release should not be deleted while a disc is still linked"


class TestReleaseDiscRelationshipIntegrity:
    """Test relationship integrity between releases and discs."""
    
    def test_disc_with_deleted_release_has_null_release_id(self, test_db):
        """Test that disc.release_id is set to NULL when release is deleted."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc = _make_disc(session, release)
            
            disc_id = disc.id
            original_release_id = release.id
            session.commit()
            
            # Verify disc is linked
            assert disc.release_id == original_release_id
            
            # Delete release
            session.delete(release)
            session.commit()
            
            # Refresh disc to get updated state
            session.expire(disc)
            disc_after = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
            
            # Disc should have NULL release_id (not deleted)
            assert disc_after is not None, "Disc should not be deleted"
            assert disc_after.release_id is None, "Disc release_id should be NULL after release deletion"
    
    def test_disc_titles_survive_release_deletion(self, test_db):
        """Test that disc titles survive when release is deleted."""
        with test_db() as session:
            movie = _make_movie(session)
            release = _make_release(session, movie)
            disc = _make_disc(session, release)
            title1 = _make_disc_title(session, disc, "title00.mkv")
            title2 = _make_disc_title(session, disc, "title01.mkv")
            
            disc_id = disc.id
            title_ids = [title1.id, title2.id]
            session.commit()
            
            # Delete release
            session.delete(release)
            session.commit()
            
            # Disc should still exist
            disc_after = session.query(models.Disc).filter(models.Disc.id == disc_id).first()
            assert disc_after is not None, "Disc should not be deleted"
            
            # Titles should still exist
            titles_after = session.query(models.DiscTitle).filter(
                models.DiscTitle.id.in_(title_ids)
            ).all()
            assert len(titles_after) == 2, f"Expected 2 titles, found {len(titles_after)}"
