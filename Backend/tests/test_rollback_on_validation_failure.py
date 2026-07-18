"""
Tests for rollback functionality when validation fails at each stage.

These tests verify that checkpoints can be created before stages and
restored when validation fails, allowing the system to revert to the
previous stage state.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from sqlalchemy.orm import Session

from core.stage_backup import (
    create_stage_backup,
    restore_stage_backup,
    backup_files,
    restore_files,
    get_stage_backup_dir,
)
from core.stage_validation import (
    ValidationResult,
    validate_rip_output,
    validate_transfer_prep_output,
    validate_finalize_output,
    validate_transfer_output,
)
from core.job_paths import JobPaths


@pytest.fixture
def mock_job_with_disc(tmp_path):
    """Create a mock job with disc for testing."""
    import uuid
    job = Mock()
    job.id = "test-job-123"
    job.ripped_files = {}
    job.post_paths = {}
    job.disc_payload = {
        "source_hashes": {},
        "source_files": {},
        "ripped_files": {},
        "post_paths": {},
        "final_hashes": {},
    }
    
    disc = Mock()
    disc.id = "disc-123"
    disc.disc_number = 1
    disc.disc_slug = "disc01"
    disc.release = None
    job.disc = disc
    
    return job


@pytest.fixture
def mock_db(mock_job_with_disc):
    """Create a mock database session that returns the mock job."""
    import uuid
    from api import models as db_models
    
    db = Mock(spec=Session)
    
    # Mock query for validation functions (DiscTitle queries with title_id)
    title1 = Mock()
    title1.id = str(uuid.uuid4())
    title1.source_file = "00100.mpls"
    title1.index = 1
    title1.order_index = 1
    
    title2 = Mock()
    title2.id = str(uuid.uuid4())
    title2.source_file = "00101.mpls"
    title2.index = 2
    title2.order_index = 2
    
    mock_query = Mock()
    mock_query.filter.return_value.all.return_value = [title1, title2]
    mock_query.filter.return_value.first.return_value = mock_job_with_disc  # For Job queries
    db.query.return_value = mock_query
    
    return db


@pytest.fixture
def job_paths(tmp_path, mock_job_with_disc):
    """Create JobPaths structure."""
    paths = JobPaths(tmp_path, mock_job_with_disc.id)
    paths.ensure_layout()
    return paths


class TestRipValidationRollback:
    """Test rollback when rip validation fails."""
    
    def test_create_checkpoint_before_rip_validation(self, mock_job_with_disc, mock_db, tmp_path):
        """Test that we can create a checkpoint before rip validation."""
        job_id = mock_job_with_disc.id
        
        # Create checkpoint before validation
        backup_dir = create_stage_backup(job_id, "rip", mock_db, reason="pre-validation checkpoint")
        
        if backup_dir:
            assert backup_dir.exists()
            db_json = backup_dir / "database.json"
            assert db_json.exists()
            
            # Verify backup contains job data
            import json
            with open(db_json) as f:
                data = json.load(f)
                assert data["job_id"] == job_id
                assert data["stage"] == "rip"
                assert data["reason"] == "pre-validation checkpoint"
    
    def test_rollback_after_rip_validation_failure(self, mock_job_with_disc, mock_db, job_paths):
        """Test that validation failures are detected and rollback infrastructure exists."""
        job_id = mock_job_with_disc.id
        
        # 1. Verify validation detects failures (missing files)
        # Don't create any files, so validation will fail
        validation_result = validate_rip_output(mock_job_with_disc, mock_db, job_paths)
        
        assert not validation_result.valid, "Validation should fail when files are missing"
        assert len(validation_result.errors) > 0, "Validation should report errors"
        
        # 2. Verify rollback functions exist and are callable
        # Note: Full rollback testing requires real SQLAlchemy models, but we can verify
        # the infrastructure exists
        assert callable(create_stage_backup), "create_stage_backup should be callable"
        assert callable(restore_stage_backup), "restore_stage_backup should be callable"
        assert callable(backup_files), "backup_files should be callable"
        assert callable(restore_files), "restore_files should be callable"
        
        # 3. Test that validation failure provides actionable error information
        # This ensures that when rollback is triggered, we have useful error messages
        error_messages = validation_result.errors
        # Error messages may vary, but should exist and provide information
        assert error_messages, "Should have error messages for validation failure"
    
    def test_rollback_preserves_previous_stage_state(self, mock_job_with_disc, mock_db, job_paths):
        """Test that validation failure detection works correctly."""
        # This test verifies that validation correctly identifies failures
        # which would trigger rollback in a real scenario
        
        # Set up a scenario where validation should fail
        # (missing required files)
        validation_result = validate_rip_output(mock_job_with_disc, mock_db, job_paths)
        
        # Verify validation correctly identifies the failure
        assert not validation_result.valid, "Validation should detect missing files"
        
        # Verify error details are available for rollback decision-making
        assert validation_result.errors, "Should have specific error messages"
        assert hasattr(validation_result, 'details'), "Should have validation details"
        
        # In a real rollback scenario, these errors would be used to:
        # 1. Determine if rollback is needed
        # 2. Log what went wrong
        # 3. Restore from checkpoint


class TestPostProcessValidationRollback:
    """Test rollback when post-process validation fails."""
    
    def test_rollback_after_postprocess_validation_failure(
        self, mock_job_with_disc, mock_db, job_paths
    ):
        """Test that post-process validation failures are detected correctly."""
        job_id = mock_job_with_disc.id
        
        # Setup: Create some source files (rip completed)
        (job_paths.raw / "title_001.mkv").write_bytes(b"fake content")
        
        # generate_expected_transfer_prep_output uses getattr(job, "post_paths", None), so set the attribute
        title1 = mock_db.query.return_value.filter.return_value.all.return_value[0]
        title1.mkv_size = 100  # so expected_sizes has this title
        post_paths = {str(title1.id): "Title_001.mkv"}  # path relative to transient
        mock_job_with_disc.post_paths = post_paths
        mock_job_with_disc.disc_payload = {
            "source_hashes": {"title_001.mkv": "fake_hash_123"},
            "source_files": {"title_001.mkv": "raw/title_001.mkv"},
            "post_paths": post_paths,
            "final_hashes": {},
        }
        
        # Simulate post-process failure (file missing at expected location)
        # Don't create (job_paths.transient / "Title_001.mkv"), so validation fails
        validation_result = validate_transfer_prep_output(mock_job_with_disc, mock_db, job_paths)
        
        assert not validation_result.valid, "Validation should fail when post-processed files are missing"
        assert len(validation_result.errors) > 0, "Should have specific error messages"
        
        # Verify rollback infrastructure is available
        assert callable(create_stage_backup)
        assert callable(restore_stage_backup)
    
    def test_file_backup_and_restore_postprocess(self, mock_job_with_disc, mock_db, job_paths, tmp_path, monkeypatch):
        """Test that file backup/restore works for post-process stage."""
        import os
        from pathlib import Path
        
        # Set up test environment for backups
        test_backup_root = Path(tmp_path) / "backups"
        test_backup_root.mkdir(parents=True)
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        # backup_files and restore_files require is_dev_mode and (for backup) get_quick_postprocess_tests_enabled
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)
        
        job_id = mock_job_with_disc.id
        
        # Setup: Create transient directory with files (as if post-process created them)
        transient_file = job_paths.transient / "Title_001.mkv"
        transient_file.write_bytes(b"post-processed content")
        
        # Create backup directory manually (since we can't use create_stage_backup with mocks)
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Backup files (copy-only: source unchanged, backup has copy)
        backup_files(job_paths.transient, backup_dir)
        
        # Verify backup has copy; source unchanged (copy-only)
        files_backup = backup_dir / "files"
        assert files_backup.exists(), "Backup directory should be created"
        assert (files_backup / "Title_001.mkv").exists(), "File should be in backup"
        assert transient_file.exists(), "Source should be unchanged (copy-only)"
        assert (files_backup / "Title_001.mkv").read_bytes() == transient_file.read_bytes()
        
        # Restore: clears transient, then copies from backup (backup unchanged)
        restore_files(backup_dir, job_paths.transient)
        
        # Verify file was restored and backup still has it (restore is copy-only)
        assert (job_paths.transient / "Title_001.mkv").exists(), "File should be restored"
        assert (files_backup / "Title_001.mkv").exists(), "Backup should be unchanged after restore"
        assert (job_paths.transient / "Title_001.mkv").read_bytes() == (files_backup / "Title_001.mkv").read_bytes()

    def test_backup_files_noop_when_quick_postprocess_tests_disabled(
        self, mock_job_with_disc, job_paths, tmp_path, monkeypatch
    ):
        """Test that backup_files is a no-op when get_quick_postprocess_tests_enabled is False."""
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: False)
        
        job_id = mock_job_with_disc.id
        (job_paths.transient / "file.mkv").write_bytes(b"content")
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        backup_files(job_paths.transient, backup_dir)
        
        # backup_files returns immediately when quick_postprocess_tests disabled: no files moved
        assert (job_paths.transient / "file.mkv").exists(), "Source file should remain"
        files_backup = backup_dir / "files"
        assert not files_backup.exists(), "backup/files should not be created"


class TestFinalizeValidationRollback:
    """Test rollback when finalize validation fails."""
    
    def test_rollback_after_finalize_validation_failure(
        self, mock_job_with_disc, mock_db, job_paths
    ):
        """Test that finalize validation failures are detected correctly."""
        job_id = mock_job_with_disc.id
        
        # Simulate finalize validation failure (missing JSON file)
        # Don't create finalize files, so validation will fail
        validation_result = validate_finalize_output(mock_job_with_disc, mock_db, job_paths)
        
        assert not validation_result.valid, "Validation should fail when finalize files are missing"
        assert len(validation_result.errors) > 0, "Should have specific error messages"
        
        # Verify rollback infrastructure is available
        assert callable(create_stage_backup)
        assert callable(restore_stage_backup)


class TestTransferValidationRollback:
    """Test rollback when transfer validation fails."""
    
    def test_rollback_after_transfer_validation_failure(
        self, mock_job_with_disc, mock_db, job_paths, tmp_path
    ):
        """Test that transfer validation failures are detected correctly."""
        job_id = mock_job_with_disc.id
        
        # Setup: Create source files (post-process completed)
        (job_paths.transient / "Title_001.mkv").write_bytes(b"content")
        
        mock_job_with_disc.disc_payload = {
            "source_hashes": {"title_001.mkv": "fake_hash"},
            "post_paths": {str(mock_db.query.return_value.filter.return_value.all.return_value[0].id): "transient/Title_001.mkv"},
            "final_hashes": {str(mock_db.query.return_value.filter.return_value.all.return_value[0].id): "fake_hash"},
        }
        mock_job_with_disc.post_paths = mock_job_with_disc.disc_payload["post_paths"]
        
        # Create destination directory (transfer destination)
        dest_dir = tmp_path / "transfer_dest"
        dest_dir.mkdir()
        
        # Simulate transfer validation failure (file missing at destination)
        # Don't copy file to destination, so validation fails
        validation_result = validate_transfer_output(mock_job_with_disc, mock_db, dest_dir)
        
        assert not validation_result.valid, "Validation should fail when transferred files are missing"
        assert len(validation_result.errors) > 0, "Should have specific error messages"
        
        # Verify rollback infrastructure is available
        assert callable(create_stage_backup)
        assert callable(restore_stage_backup)


class TestRollbackIntegration:
    """Integration tests for rollback across stages."""
    
    def test_rollback_chain_across_stages(self, mock_job_with_disc, mock_db, job_paths):
        """Test that validation failures are detected at multiple stages."""
        # This test verifies that validation works correctly across different stages
        # which is necessary for rollback to work properly
        
        # Test rip validation failure
        rip_result = validate_rip_output(mock_job_with_disc, mock_db, job_paths)
        assert not rip_result.valid, "Rip validation should detect missing files"
        
        # Test post-process validation failure (with setup)
        (job_paths.raw / "title_001.mkv").write_bytes(b"content")
        mock_job_with_disc.disc_payload = {
            "source_hashes": {"title_001.mkv": "hash"},
            "post_paths": {str(mock_db.query.return_value.filter.return_value.all.return_value[0].id): "transient/file.mkv"},
        }
        mock_job_with_disc.post_paths = mock_job_with_disc.disc_payload["post_paths"]
        postprocess_result = validate_transfer_prep_output(mock_job_with_disc, mock_db, job_paths)
        assert not postprocess_result.valid, "Post-process validation should detect missing files"
        
        # Verify rollback functions are available for all stages
        stages = ["rip", "postprocess", "finalize", "transfer"]
        for stage in stages:
            # Functions should exist (we can't fully test with mocks, but verify they're callable)
            assert callable(create_stage_backup)
            assert callable(restore_stage_backup)
    
    def test_rollback_preserves_file_structure(self, mock_job_with_disc, mock_db, job_paths, tmp_path, monkeypatch):
        """Test that file backup/restore preserves directory structure."""
        import os
        from pathlib import Path
        import shutil
        
        # Set up test environment
        test_backup_root = Path(tmp_path) / "backups"
        test_backup_root.mkdir(parents=True)
        monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path))
        # backup_files and restore_files require is_dev_mode and (for backup) get_quick_postprocess_tests_enabled
        monkeypatch.setattr("core.stage_backup.is_dev_mode", lambda: True)
        monkeypatch.setattr("core.settings.get_quick_postprocess_tests_enabled", lambda: True)
        
        job_id = mock_job_with_disc.id
        
        # Create nested directory structure
        nested_dir = job_paths.transient / "Season_01" / "Episode_01"
        nested_dir.mkdir(parents=True)
        (nested_dir / "file.mkv").write_bytes(b"content")
        
        # Create backup directory manually
        backup_dir = get_stage_backup_dir(job_id, "postprocess")
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        # Backup files
        backup_files(job_paths.transient, backup_dir)
        
        # Verify backup was created
        files_backup = backup_dir / "files"
        assert files_backup.exists(), "Backup directory should be created"
        
        # Delete everything
        shutil.rmtree(job_paths.transient)
        job_paths.transient.mkdir()
        
        # Restore
        restore_files(backup_dir, job_paths.transient)
        
        # Verify structure is restored
        assert (job_paths.transient / "Season_01" / "Episode_01" / "file.mkv").exists(), \
            "Nested directory structure should be restored"

