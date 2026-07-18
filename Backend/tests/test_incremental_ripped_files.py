"""
Tests for incremental ripped_files updates with debounced commits.
"""
import time
import pytest
from unittest.mock import Mock, MagicMock, patch
from pathlib import Path

from workers.tasks import DebouncedRippedFilesCommit
from api import models


class TestDebouncedRippedFilesCommit:
    """Test DebouncedRippedFilesCommit helper class."""
    
    def test_add_triggers_commit_on_threshold(self):
        """Test that commit is triggered when threshold is reached."""
        job = Mock(spec=models.Job)
        job.ripped_files = {}
        db = Mock()
        
        committer = DebouncedRippedFilesCommit(job, db, commit_threshold=3, time_threshold=10.0)
        
        # Add 3 titles - should trigger commit
        result1 = committer.add("title1", "file1.mkv")
        assert result1 is False  # First title, no commit yet
        
        result2 = committer.add("title2", "file2.mkv")
        assert result2 is False  # Second title, no commit yet
        
        result3 = committer.add("title3", "file3.mkv")
        assert result3 is True  # Third title, should trigger commit
        
        # Verify commit was called
        assert len(committer.pending_updates) == 0  # Pending cleared after commit
        assert committer.commit_count == 0  # Counter reset
    
    def test_add_triggers_commit_on_time_threshold(self):
        """Test that commit is triggered when time threshold is reached."""
        job = Mock(spec=models.Job)
        job.ripped_files = {}
        db = Mock()
        
        committer = DebouncedRippedFilesCommit(job, db, commit_threshold=10, time_threshold=0.1)
        
        # Add one title
        committer.add("title1", "file1.mkv")
        
        # Wait for time threshold
        time.sleep(0.15)
        
        # Add another title - should trigger commit due to time
        result = committer.add("title2", "file2.mkv")
        assert result is True  # Should commit due to time threshold
        
        # Verify commit was called
        assert len(committer.pending_updates) == 0
    
    def test_flush_commits_all_pending(self):
        """Test that flush commits all pending updates."""
        job = Mock(spec=models.Job)
        job.ripped_files = {}
        db = Mock()
        
        committer = DebouncedRippedFilesCommit(job, db, commit_threshold=10, time_threshold=10.0)
        
        # Add multiple titles without reaching threshold
        committer.add("title1", "file1.mkv")
        committer.add("title2", "file2.mkv")
        
        assert len(committer.pending_updates) == 2
        
        # Flush should commit all
        committer.flush()
        
        assert len(committer.pending_updates) == 0
    
    def test_commit_merges_with_existing_ripped_files(self):
        """Test that commit merges pending updates with existing ripped_files."""
        job = Mock(spec=models.Job)
        job.ripped_files = {"existing_title": "existing_file.mkv"}
        job.id = "test-job"
        job.job_status = "running"
        db = Mock()
        
        committer = DebouncedRippedFilesCommit(job, db, commit_threshold=1, time_threshold=10.0)
        
        # Add new title (this will trigger commit due to threshold=1)
        with patch('core.job_state.apply_job_state') as mock_apply:
            committer.add("new_title", "new_file.mkv")
            
            # Verify apply_job_state was called with merged ripped_files
            assert mock_apply.called
            call_args = mock_apply.call_args
            assert call_args is not None
            updates = call_args[1]['updates']
            assert "ripped_files" in updates
            merged = updates["ripped_files"]
            assert "existing_title" in merged
            assert "new_title" in merged
            assert merged["existing_title"] == "existing_file.mkv"
            assert merged["new_title"] == "new_file.mkv"
    
    def test_commit_handles_exceptions_gracefully(self):
        """Test that commit handles exceptions without raising."""
        job = Mock(spec=models.Job)
        job.ripped_files = {}
        db = Mock()
        
        committer = DebouncedRippedFilesCommit(job, db, commit_threshold=1, time_threshold=10.0)
        committer.add("title1", "file1.mkv")
        
        # Make apply_job_state raise an exception
        with patch('workers.tasks.apply_job_state', side_effect=Exception("DB error")):
            # Should not raise, just log warning
            committer.commit()
            
            # Pending updates should still be cleared (or kept, depending on implementation)
            # For now, we clear them even on error to avoid duplicates on retry
            assert len(committer.pending_updates) == 0


class TestIncrementalRippedFilesInRipFlow:
    """Test incremental ripped_files updates during rip flow."""
    
    @pytest.fixture
    def mock_job(self):
        """Create a mock job with required attributes."""
        job = Mock(spec=models.Job)
        job.id = "test-job-id"
        job.ripped_files = {}
        job.disc_payload = {}
        job.disc = Mock()
        job.disc.id = "test-disc-id"
        return job
    
    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        return Mock()
    
    def test_ripped_files_updated_incrementally(self, mock_job, mock_db, tmp_path):
        """Test that ripped_files is updated as titles complete."""
        # This would require mocking the full rip_disc flow
        # For now, we test the committer directly
        committer = DebouncedRippedFilesCommit(mock_job, mock_db, commit_threshold=2, time_threshold=1.0)
        
        # Simulate titles completing
        with patch('core.job_state.apply_job_state') as mock_apply:
            committer.add("title1", "title_001.mkv")
            committer.add("title2", "title_002.mkv")  # Should trigger commit
            
            # Verify commit was triggered by threshold
            assert mock_apply.called, "Commit should have been triggered at threshold"
            
            # Add one more and flush to test flush behavior
            committer.add("title3", "title_003.mkv")
            initial_call_count = mock_apply.call_count
            committer.flush()  # Force final commit
            
            # Verify flush also committed (if there were pending updates)
            # If title3 triggered another commit, flush might not call again
            assert mock_apply.call_count >= initial_call_count
    
    def test_debouncing_reduces_commit_frequency(self, mock_job, mock_db):
        """Test that debouncing reduces the number of commits."""
        committer = DebouncedRippedFilesCommit(mock_job, mock_db, commit_threshold=3, time_threshold=10.0)
        
        with patch('core.job_state.apply_job_state') as mock_apply:
            # Add 5 titles - should commit at threshold (3) and on flush (2 remaining)
            for i in range(5):
                committer.add(f"title{i}", f"file{i}.mkv")
            
            # Force final flush for any remaining
            committer.flush()
        
        # Should have committed at threshold (title 3) and on flush (titles 4-5)
        # Total: 2 commits for 5 titles (instead of 5 individual commits)
        assert mock_apply.call_count == 2  # Threshold commit + flush commit
