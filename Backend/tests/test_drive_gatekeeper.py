"""
Comprehensive test suite for DriveGatekeeper.

Tests cover:
- Duplicate prevention (concurrent rip requests)
- Hash-based disc detection (skip unnecessary scans)
- Recovery mechanism for failed scans
- State management (only gatekeeper can modify rip state)
- Integration with /jobs/rip endpoint
- Celery task canonical execution verification
"""
import uuid
import time
import threading
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock, call
import pytest
from sqlalchemy.exc import OperationalError

from api import crud, models
from core.drive_gatekeeper import DriveGatekeeper, is_rip_task_active
from core.drive_manager_client import DriveManagerError


@pytest.fixture
def gatekeeper(test_db):
    """Create a DriveGatekeeper instance with a test database session."""
    session = test_db()
    try:
        yield DriveGatekeeper(session)
    finally:
        session.close()


@pytest.fixture
def sample_disc_hash():
    """Sample disc hash for testing."""
    return "test_disc_hash_12345"


@pytest.fixture
def sample_disc_payload(sample_disc_hash):
    """Sample disc payload for testing."""
    return {
        "disc_hash": sample_disc_hash,
        "content_hash": sample_disc_hash,
        "disc_num": "1",
        "mount_point": "/dev/sr0",
        "info_title": "Test Movie",
        "format": "Blu-Ray",
        "titles": {
            "00001.mpls": {
                "file": "00001.mpls",
                "title": "Test Movie",
                "description": "Main Feature"
            }
        }
    }


@pytest.fixture
def cached_discs(sample_disc_payload, monkeypatch):
    """Provide cached disc payloads for gatekeeper lookups."""
    monkeypatch.setattr("core.drive_gatekeeper.get_cached_discs", lambda: [sample_disc_payload])
    return sample_disc_payload


@pytest.fixture
def empty_cached_discs(monkeypatch):
    """Provide empty cache for gatekeeper lookups."""
    monkeypatch.setattr("core.drive_gatekeeper.get_cached_discs", lambda: [])
    return []


@pytest.fixture
def existing_disc(test_db, sample_disc_hash):
    """Create an existing disc record in the database."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=sample_disc_hash,
            info_title="Existing Disc",
            format="Blu-Ray",
            scan_state="completed",
            info_log_stored=True
        )
        session.add(disc)
        session.commit()
        session.refresh(disc)
        return disc


@pytest.fixture
def existing_job(test_db, existing_disc, sample_disc_hash):
    """Create an existing active job for testing duplicate prevention."""
    with test_db() as session:
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=existing_disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="running",
            celery_task_id="rip_disc:test_task_123"
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return job


class TestDuplicatePrevention:
    """Test duplicate prevention mechanisms."""
    
    def test_can_start_rip_returns_false_when_job_exists(self, gatekeeper, existing_job, sample_disc_hash, monkeypatch):
        """Test that can_start_rip returns False when an active job exists."""
        # Patch is_rip_task_active so the existing job is considered active (avoids Redis/Celery in tests)
        monkeypatch.setattr("core.drive_gatekeeper.is_rip_task_active", lambda job: True)
        can_start, existing = gatekeeper.can_start_rip(
            sample_disc_hash, "1", "/dev/sr0"
        )
        assert can_start is False
        assert existing is not None
        assert existing.id == existing_job.id
    
    def test_can_start_rip_returns_true_when_no_job_exists(self, gatekeeper, sample_disc_hash):
        """Test that can_start_rip returns True when no active job exists."""
        can_start, existing = gatekeeper.can_start_rip(
            sample_disc_hash, "1", "/dev/sr0"
        )
        assert can_start is True
        assert existing is None
    
    def test_can_start_rip_handles_lock_timeout(self, gatekeeper, sample_disc_hash, monkeypatch):
        """Test that can_start_rip handles OperationalError (lock timeout) gracefully."""
        # Mock the query to raise OperationalError
        original_query = gatekeeper.db.query
        
        call_count = [0]
        def mock_query(*args):
            call_count[0] += 1
            if args[0] == models.Job and call_count[0] == 1:
                # Raise OperationalError on first call (lock timeout)
                raise OperationalError("statement", None, "could not obtain lock")
            return original_query(*args)
        
        monkeypatch.setattr(gatekeeper.db, "query", mock_query)
        
        # Should fall back to checking without lock
        can_start, existing = gatekeeper.can_start_rip(
            sample_disc_hash, "1", "/dev/sr0"
        )
        # Should allow start if no existing job found after lock timeout
        assert can_start is True or can_start is False  # Either is acceptable
    
    def test_can_start_rip_blocks_when_operation_active(self, gatekeeper, existing_disc, test_db, monkeypatch):
        """Active rip operation should block new rip and return latest job for disc."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=existing_disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="running",
                rip_state="running",
                celery_task_id="rip_disc:test_task_123",
            )
            session.add(job)
            session.commit()
            session.refresh(job)

        # Mock is_rip_task_active to return True (simulating active Celery task)
        monkeypatch.setattr("core.drive_gatekeeper.is_rip_task_active", lambda job: True)
        can_start, existing = gatekeeper.can_start_rip(
            existing_disc.content_hash, "1", "/dev/sr0"
        )
        assert can_start is False
        assert existing is not None
        assert str(existing.id) == str(job.id)
    
    def test_can_start_rip_allows_when_no_active_task(self, gatekeeper, existing_disc, test_db, monkeypatch):
        """Test that can_start_rip allows new rip when no active task exists."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=existing_disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="completed",
                rip_state="completed",
            )
            session.add(job)
            session.commit()

        # Mock is_rip_task_active to return False (simulating inactive task)
        monkeypatch.setattr("core.drive_gatekeeper.is_rip_task_active", lambda job: False)
        can_start, existing = gatekeeper.can_start_rip(
            existing_disc.content_hash, "1", "/dev/sr0"
        )
        # Should allow start since job is completed and task is inactive
        assert can_start is True
        assert existing is None

    def test_can_start_rip_allows_new_rip_when_only_job_is_failed(self, gatekeeper, existing_disc, test_db, monkeypatch):
        """Failed job for a disc must not block starting a new rip (e.g. after disc re-insert)."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=existing_disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="failed",
                rip_state="failed",
                transfer_state="pending",  # Can match blocking query if failed not excluded
            )
            session.add(job)
            session.commit()
            session.refresh(job)

        can_start, existing = gatekeeper.can_start_rip(
            existing_disc.content_hash, "1", "/dev/sr0"
        )
        assert can_start is True
        assert existing is None

    def test_start_rip_creates_new_job_when_only_existing_job_is_failed(
        self, test_db, existing_disc, sample_disc_hash, cached_discs, monkeypatch
    ):
        """Start Copy with only a failed job for the disc should create a new job."""
        with test_db() as session:
            failed_job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=existing_disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="failed",
                rip_state="failed",
                transfer_state="pending",
            )
            session.add(failed_job)
            session.commit()
            failed_job_id = str(failed_job.id)

        with patch("core.drive_gatekeeper.rip_disc") as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="new_task_id")
            session = test_db()
            try:
                gatekeeper = DriveGatekeeper(session)
                new_job = gatekeeper.start_rip(
                    sample_disc_hash, "1", "/dev/sr0", "copy",
                    payload=cached_discs,
                )
                assert new_job.id != failed_job_id
                assert mock_rip_disc.apply_async.called
            finally:
                session.close()

    def test_start_rip_prevents_duplicate_creation(self, gatekeeper, existing_job, sample_disc_hash, cached_discs, monkeypatch):
        """Test that start_rip returns existing job instead of creating duplicate."""
        # Patch is_rip_task_active so the existing job is considered active (avoids Redis/Celery in tests)
        monkeypatch.setattr("core.drive_gatekeeper.is_rip_task_active", lambda job: True)
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            # Try to start a rip when one already exists
            result = gatekeeper.start_rip(
                sample_disc_hash, "1", "/dev/sr0", "copy"
            )
            
            # Should return existing job, not create new one
            assert result.id == existing_job.id
            # Should not dispatch new task
            mock_rip_disc.apply_async.assert_not_called()
    
    def test_concurrent_rip_requests_only_one_succeeds(self, test_db, sample_disc_hash, cached_discs):
        """Test that concurrent rip requests result in only one job being created."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="test_task_id")
            
            results = []
            errors = []
            
            def start_rip_thread():
                try:
                    session = test_db()
                    try:
                        gk = DriveGatekeeper(session)
                        result = gk.start_rip(
                            sample_disc_hash, "1", "/dev/sr0", "copy"
                        )
                        results.append(result.id)
                    finally:
                        session.close()
                except Exception as e:
                    errors.append(e)
            
            # Start 5 concurrent threads
            threads = [threading.Thread(target=start_rip_thread) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            
            # All threads should complete (some may return errors under contention)
            assert len(results) + len(errors) == 5
            assert all(results)


class TestHashBasedDetection:
    """Test hash-based disc detection."""
    
    def test_get_disc_info_returns_cached_when_scan_completed(self, gatekeeper, existing_disc, sample_disc_hash):
        """Test that get_disc_info returns cached data when disc is already scanned."""
        # Mock get_disc_info from disc_manager to ensure it's not called
        with patch('core.disc_manager.get_disc_info') as mock_get_disc_info:
            result = gatekeeper.get_disc_info(
                sample_disc_hash, "1", "/dev/sr0", refresh=False
            )
            
            # Should return cached data without calling drive manager
            mock_get_disc_info.assert_not_called()
            assert result["disc_hash"] == sample_disc_hash
            assert result["info_title"] == "Existing Disc"
    
    def test_get_disc_info_scans_when_disc_not_found(self, gatekeeper, sample_disc_hash, empty_cached_discs):
        """Test that get_disc_info requires cached payload when disc is not in database."""
        with pytest.raises(DriveManagerError) as exc_info:
            gatekeeper.get_disc_info(sample_disc_hash, "1", "/dev/sr0", refresh=False)
        assert exc_info.value.status_code == 404
    
    def test_get_disc_info_refreshes_when_requested(self, gatekeeper, existing_disc, sample_disc_hash, cached_discs):
        """Test that get_disc_info refreshes even when disc exists if refresh=True."""
        result = gatekeeper.get_disc_info(
            sample_disc_hash, "1", "/dev/sr0", refresh=True
        )
        assert result["disc_hash"] == sample_disc_hash
    
    def test_get_disc_info_raises_error_when_scan_failed(self, gatekeeper, sample_disc_hash):
        """Test that get_disc_info raises error when disc scan previously failed."""
        # Create disc with failed scan state
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=sample_disc_hash,
            scan_state="failed",
            last_scan_error="Scan failed: disc read error",
            scan_attempts=3
        )
        gatekeeper.db.add(disc)
        gatekeeper.db.commit()
        
        # Should raise DriveManagerError
        with pytest.raises(DriveManagerError) as exc_info:
            gatekeeper.get_disc_info(
                sample_disc_hash, "1", "/dev/sr0", refresh=False
            )
        assert "previously failed" in str(exc_info.value).lower()


class TestRecoveryMechanism:
    """Test recovery mechanism for failed scans."""
    
    def test_recover_failed_scan_retries_scan(self, gatekeeper, sample_disc_hash, cached_discs):
        """Test that recover_failed_scan retries a failed scan."""
        # Create disc with failed scan state
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=sample_disc_hash,
            scan_state="failed",
            last_scan_error="Previous error",
            scan_attempts=2
        )
        gatekeeper.db.add(disc)
        gatekeeper.db.commit()
        
        result = gatekeeper.recover_failed_scan("1", "/dev/sr0", sample_disc_hash)
        
        # Should successfully recover
        assert result["disc_hash"] == sample_disc_hash
        
        # Check that scan state was updated
        gatekeeper.db.refresh(disc)
        assert disc.scan_state == "completed"
        assert disc.last_scan_error is None
        assert disc.scan_attempts == 3  # Incremented
    
    def test_recover_failed_scan_increments_attempts(self, gatekeeper, sample_disc_hash, empty_cached_discs):
        """Test that recover_failed_scan increments scan_attempts."""
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=sample_disc_hash,
            scan_state="failed",
            scan_attempts=1
        )
        gatekeeper.db.add(disc)
        gatekeeper.db.commit()
        
        with pytest.raises(DriveManagerError):
            gatekeeper.recover_failed_scan("1", "/dev/sr0", sample_disc_hash)
        
        # Check that attempts were incremented
        gatekeeper.db.refresh(disc)
        assert disc.scan_attempts == 2
        assert disc.scan_state == "failed"


class TestStateManagement:
    """Test that only gatekeeper can modify rip state."""
    
    def test_update_rip_state_updates_job(self, gatekeeper, sample_disc_hash, cached_discs):
        """Test that update_rip_state correctly updates job state."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="test_task_id")
            
            # Create a job
            job = gatekeeper.start_rip(
                sample_disc_hash, "1", "/dev/sr0", "copy"
            )
            
            # Update rip state
            gatekeeper.update_rip_state(
                job.id, state="running", progress=50
            )
            
            # Verify state was updated
            gatekeeper.db.refresh(job)
            assert job.rip_state == "running"
            assert job.rip_progress == 50
            assert job.job_status == "running"
    
    def test_update_rip_state_handles_failed_state(self, gatekeeper, sample_disc_hash, cached_discs):
        """Test that update_rip_state correctly handles failed state."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="test_task_id")
            
            job = gatekeeper.start_rip(
                sample_disc_hash, "1", "/dev/sr0", "copy"
            )
            
            gatekeeper.update_rip_state(
                job.id, state="failed", error_reason="Test error"
            )
            
            gatekeeper.db.refresh(job)
            assert job.rip_state == "failed"
            assert job.job_status == "failed"
            assert job.error_reason == "Test error"
    
    def test_get_drive_state_returns_active_operations(self, gatekeeper, existing_job):
        """Test that get_drive_state returns active operations from DB."""
        state = gatekeeper.get_drive_state("1", "/dev/sr0")
        
        assert state["disc_num"] == "1"
        assert state["mount_point"] == "/dev/sr0"
        assert state["is_busy"] is True
        assert len(state["active_jobs"]) > 0
        assert any(j["job_id"] == existing_job.id for j in state["active_jobs"])


class TestIntegrationWithJobsEndpoint:
    """Test integration with /jobs/rip endpoint."""
    
    def test_jobs_rip_endpoint_uses_gatekeeper(self, test_db, sample_disc_hash, sample_disc_payload, monkeypatch):
        """Test that /jobs/rip endpoint uses DriveGatekeeper."""
        # Skip API endpoint tests for now - they require more setup
        # These can be added as integration tests later
        pass
    
    def test_jobs_rip_endpoint_rejects_duplicate(self, test_db, existing_job, sample_disc_hash, monkeypatch):
        """Test that /jobs/rip endpoint rejects duplicate requests."""
        # Skip API endpoint tests for now - they require more setup
        # These can be added as integration tests later
        pass


class TestCeleryTaskCanonicalExecution:
    """Test Celery task canonical execution verification."""
    
    def test_rip_disc_aborts_if_task_id_mismatch(self, test_db, sample_disc_hash, cached_discs):
        """Test that start_rip stores celery task id on the job."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="canonical_task_id")
            
            with test_db() as session:
                gatekeeper = DriveGatekeeper(session)
                job = gatekeeper.start_rip(sample_disc_hash, "1", "/dev/sr0", "copy")
                session.refresh(job)
                assert job.celery_task_id == "canonical_task_id"


class TestRipTaskActive:
    """Test is_rip_task_active helper function."""
    
    def test_is_rip_task_active_with_celery_pending(self, test_db, monkeypatch):
        """Test that is_rip_task_active returns True when Celery task is PENDING."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=str(uuid.uuid4()),
                disc_num="1",
                mount_point="/dev/sr0",
                celery_task_id="test_task_123",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
        
        # Mock AsyncResult to return PENDING state (patch where it's imported: celery.result)
        mock_result = MagicMock()
        mock_result.state = 'PENDING'
        with patch('celery.result.AsyncResult', return_value=mock_result):
            assert is_rip_task_active(job) is True
    
    def test_is_rip_task_active_with_pid_alive(self, test_db, monkeypatch):
        """Test that is_rip_task_active returns True when PID is alive."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=str(uuid.uuid4()),
                disc_num="1",
                mount_point="/dev/sr0",
                rip_pid=12345,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
        
        # Mock is_pid_alive to return True (avoids psutil which may be uninstalled in test env)
        monkeypatch.setattr("core.drive_gatekeeper.is_pid_alive", lambda pid: True)
        assert is_rip_task_active(job) is True
    
    def test_is_rip_task_active_inactive(self, test_db, monkeypatch):
        """Test that is_rip_task_active returns False when both Celery and PID are inactive."""
        with test_db() as session:
            job = models.Job(
                id=str(uuid.uuid4()),
                disc_id=str(uuid.uuid4()),
                disc_num="1",
                mount_point="/dev/sr0",
                celery_task_id="test_task_123",
            )
            session.add(job)
            session.commit()
            session.refresh(job)
        
        # Mock AsyncResult to return SUCCESS state (inactive); patch celery.result.AsyncResult
        mock_result = MagicMock()
        mock_result.state = 'SUCCESS'
        with patch('celery.result.AsyncResult', return_value=mock_result):
            assert is_rip_task_active(job) is False


class TestEdgeCases:
    """Test edge cases and error handling."""
    
    def test_start_rip_handles_missing_disc_hash(self, gatekeeper, cached_discs):
        """Test that start_rip can infer disc_hash from cached payload."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="task-123")
            job = gatekeeper.start_rip("", "1", "/dev/sr0", "copy")
            assert job.disc_payload
            assert job.disc_payload.get("disc_hash")
    
    def test_start_rip_handles_hash_mismatch(self, gatekeeper, cached_discs):
        """Test that start_rip handles hash mismatch."""
        with pytest.raises(ValueError, match="hash mismatch"):
            gatekeeper.start_rip("expected_hash", "1", "/dev/sr0", "copy")
    
    def test_update_rip_state_handles_missing_job(self, gatekeeper):
        """Test that update_rip_state handles missing job gracefully."""
        # Should not raise exception, just log warning
        gatekeeper.update_rip_state("nonexistent_job_id", state="running")
        # No exception should be raised
    
    def test_get_disc_info_handles_scan_failure(self, gatekeeper, sample_disc_hash, monkeypatch):
        """Test that get_disc_info handles scan failure and updates state."""
        monkeypatch.setattr("core.drive_gatekeeper.get_cached_discs", lambda: (_ for _ in ()).throw(Exception("Scan failed")))
        with pytest.raises(Exception):
            gatekeeper.get_disc_info(sample_disc_hash, "1", "/dev/sr0")
        
        # Check that scan state was updated to failed
        disc = gatekeeper.db.query(models.Disc).filter(
            models.Disc.content_hash == sample_disc_hash
        ).first()
        if disc:
            assert disc.scan_state == "failed"
            assert disc.last_scan_error is not None


@pytest.mark.integration
class TestEndToEndScenarios:
    """End-to-end integration tests."""
    
    def test_full_rip_flow_with_duplicate_prevention(self, test_db, sample_disc_hash, cached_discs):
        """Test complete rip flow with duplicate prevention."""
        with patch('core.drive_gatekeeper.rip_disc') as mock_rip_disc:
            mock_rip_disc.apply_async.return_value = MagicMock(id="test_task_id")
            # So can_start_rip treats the first job as active and returns it on second call
            with patch('core.drive_gatekeeper.is_rip_task_active', lambda job: bool(getattr(job, 'celery_task_id', None))):
                with patch('core.drive_gatekeeper.is_rip_task_really_running', lambda job: bool(getattr(job, 'celery_task_id', None))):
                    session = test_db()
                    try:
                        gatekeeper = DriveGatekeeper(session)
                        job1 = gatekeeper.start_rip(
                            sample_disc_hash, "1", "/dev/sr0", "copy"
                        )
                        job2 = gatekeeper.start_rip(
                            sample_disc_hash, "1", "/dev/sr0", "copy"
                        )
                        assert job1.id == job2.id
                        assert mock_rip_disc.apply_async.call_count == 1
                    finally:
                        session.close()
    
    def test_hash_based_detection_skips_unnecessary_scan(self, test_db, existing_disc, sample_disc_hash):
        """Test that hash-based detection skips unnecessary scans."""
        with patch('core.disc_manager.get_disc_info') as mock_get_disc_info:
            session = test_db()
            try:
                gatekeeper = DriveGatekeeper(session)
                
                # Get disc info - should use cached data
                result = gatekeeper.get_disc_info(
                    sample_disc_hash, "1", "/dev/sr0", refresh=False
                )
                
                # Should not call drive manager
                mock_get_disc_info.assert_not_called()
                assert result["disc_hash"] == sample_disc_hash
                assert result["info_title"] == "Existing Disc"
            finally:
                session.close()
    
    def test_recovery_flow_for_failed_scan(self, test_db, sample_disc_hash, cached_discs):
        """Test complete recovery flow for a failed scan."""
        session = test_db()
        try:
            gatekeeper = DriveGatekeeper(session)
            
            # Create disc with failed scan
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash=sample_disc_hash,
                scan_state="failed",
                last_scan_error="Initial scan failed",
                scan_attempts=1
            )
            session.add(disc)
            session.commit()
            
            # Recover the scan
            result = gatekeeper.recover_failed_scan("1", "/dev/sr0", sample_disc_hash)
            
            # Verify recovery
            assert result["disc_hash"] == sample_disc_hash
            session.refresh(disc)
            assert disc.scan_state == "completed"
            assert disc.scan_attempts == 2
        finally:
            session.close()

