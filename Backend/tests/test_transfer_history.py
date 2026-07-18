"""
Tests for transfer history system.
"""
from api import models
from core import transfer_history


def test_log_transfer_start(test_db):
    """Test logging transfer start."""
    with test_db() as session:
        history_id = transfer_history.log_transfer_start(
            session,
            "job1",
            "config1",
            "local",
            "/source/path",
            "/dest/path"
        )
        
        assert history_id
        
        history = session.query(models.TransferHistory).filter(
            models.TransferHistory.id == history_id
        ).first()
        
        assert history is not None
        assert history.job_id == "job1"
        assert history.status == "running"


def test_log_transfer_progress(test_db):
    """Test logging transfer progress."""
    with test_db() as session:
        history_id = transfer_history.log_transfer_start(
            session,
            "job1",
            "config1",
            "local",
            "/source",
            "/dest"
        )
        
        transfer_history.log_transfer_progress(session, history_id, 1024 * 1024, 10.5)
        
        history = session.query(models.TransferHistory).filter(
            models.TransferHistory.id == history_id
        ).first()
        
        assert history.bytes_transferred == 1024 * 1024
        assert history.average_speed_mbps == 10.5


def test_log_transfer_complete(test_db):
    """Test logging transfer completion."""
    with test_db() as session:
        history_id = transfer_history.log_transfer_start(
            session,
            "job1",
            "config1",
            "local",
            "/source",
            "/dest"
        )
        
        transfer_history.log_transfer_complete(
            session,
            history_id,
            1024 * 1024,
            10.0,
            True,
            "abc123"
        )
        
        history = session.query(models.TransferHistory).filter(
            models.TransferHistory.id == history_id
        ).first()
        
        assert history.status == "completed"
        assert history.verification_status == "verified"
        assert history.verification_hash == "abc123"


def test_log_transfer_failed(test_db):
    """Test logging transfer failure."""
    with test_db() as session:
        history_id = transfer_history.log_transfer_start(
            session,
            "job1",
            "config1",
            "local",
            "/source",
            "/dest"
        )
        
        transfer_history.log_transfer_failed(session, history_id, "Test error")
        
        history = session.query(models.TransferHistory).filter(
            models.TransferHistory.id == history_id
        ).first()
        
        assert history.status == "failed"
        assert history.error_message == "Test error"


def test_log_transfer_deduplicated(test_db):
    """Test logging deduplicated transfer."""
    with test_db() as session:
        history_id = transfer_history.log_transfer_start(
            session,
            "job1",
            "config1",
            "local",
            "/source",
            "/dest"
        )
        
        transfer_history.log_transfer_deduplicated(session, history_id, "abc123")
        
        history = session.query(models.TransferHistory).filter(
            models.TransferHistory.id == history_id
        ).first()
        
        assert history.status == "deduplicated"
        assert history.was_deduplicated is True
        assert history.verification_hash == "abc123"


def test_get_transfer_history(test_db):
    """Test getting transfer history."""
    with test_db() as session:
        # Create some history entries
        h1 = transfer_history.log_transfer_start(session, "job1", "config1", "local", "/s1", "/d1")
        h2 = transfer_history.log_transfer_start(session, "job2", "config1", "local", "/s2", "/d2")
        
        history = transfer_history.get_transfer_history(session, limit=10)
        
        assert len(history) >= 2


def test_get_transfer_history_eager_loads_identity_chain(test_db):
    """#593: get_transfer_history must eager-load Job → Disc → Release → Movie so
    the row's human-readable identity can be resolved without an N+1."""
    with test_db() as session:
        movie = models.Movie(name="V for Vendetta", production_year=2006)
        session.add(movie)
        session.flush()
        release = models.Release(
            slug="v-for-vendetta-2006",
            type="movie",
            name="Blu-Ray Edition",
            movie_id=movie.id,
            release_year=2006,
        )
        session.add(release)
        session.flush()
        disc = models.Disc(
            content_hash="HASHV1",
            release_id=release.id,
            disc_name="V for Vendetta - Blu-Ray",
            disc_number=1,
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            id="job-vfv-1",
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="completed",
        )
        session.add(job)
        session.commit()
        h_id = transfer_history.log_transfer_start(
            session, job.id, "config1", "smb", "/src", "/dst"
        )
        transfer_history.log_transfer_complete(session, h_id, 1024 * 1024, 30.0, True)

        rows = transfer_history.get_transfer_history(session, limit=10)
        match = next((r for r in rows if r.id == h_id), None)
        assert match is not None
        # Chain reachable without firing more queries (eager-loaded).
        assert match.job is not None
        assert match.job.disc is not None
        assert match.job.disc.release is not None
        assert match.job.disc.release.movie is not None
        assert match.job.disc.release.movie.name == "V for Vendetta"
        assert match.job.disc.release.release_year == 2006
        assert match.job.disc.disc_name == "V for Vendetta - Blu-Ray"


def test_get_transfer_history_orphaned_row_keeps_identity_null(test_db):
    """#593: When the job FK was cleared (SET NULL after deletion), the row's
    identity chain returns None and the API surfaces nulls for all four name
    fields rather than 500-ing."""
    with test_db() as session:
        # No job/disc/release/movie created; this row is born orphaned.
        h_id = transfer_history.log_transfer_start(
            session, None, "config1", "smb", "/src/orphan", "/dst/orphan"
        )
        rows = transfer_history.get_transfer_history(session, limit=10)
        match = next((r for r in rows if r.id == h_id), None)
        assert match is not None
        assert match.job is None


def test_get_transfer_statistics(test_db):
    """Test getting transfer statistics."""
    with test_db() as session:
        # Create completed transfer
        h1 = transfer_history.log_transfer_start(session, "job1", "config1", "local", "/s1", "/d1")
        transfer_history.log_transfer_complete(session, h1, 1024 * 1024, 10.0, True)
        
        stats = transfer_history.get_transfer_statistics(session, days=30)
        
        assert stats["total_transfers"] >= 1
        assert stats["completed"] >= 1

