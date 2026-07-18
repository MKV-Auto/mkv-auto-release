"""
Tests for JobPaths class.
Tests path resolution and directory structure computation.
"""
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from core.job_paths import JobPaths
from core.utils import resolve_jobs_root


@pytest.fixture
def mock_job():
    """Create a mock job object for testing."""
    job = Mock()
    job.id = "test-job-123"
    return job


class TestJobPaths:
    """Test JobPaths class."""
    
    def test_from_job(self, mock_job, tmp_path):
        """Test JobPaths.from_job() uses job.id and resolve_jobs_root()."""
        jobs_root = tmp_path / "jobs"
        jobs_root.mkdir(parents=True)
        
        with patch("core.job_paths.resolve_jobs_root", return_value=jobs_root):
            paths = JobPaths.from_job(mock_job)
        
        assert paths.job_id == mock_job.id
        assert paths.jobs_root == jobs_root
        assert paths.root == jobs_root / mock_job.id
        assert paths.raw == jobs_root / mock_job.id / "raw"
        assert paths.previews == jobs_root / mock_job.id / "previews"
        assert paths.metadata == jobs_root / mock_job.id / "metadata"
        assert paths.finalize == jobs_root / mock_job.id / "finalize"
        assert paths.transient == jobs_root / mock_job.id / "transient"
    
    def test_ensure_layout(self, mock_job, tmp_path):
        """Test that ensure_layout() creates all required directories."""
        jobs_root = tmp_path / "jobs"
        
        paths = JobPaths(jobs_root, mock_job.id)
        paths.ensure_layout()
        
        assert paths.root.exists()
        assert paths.raw.exists()
        assert paths.previews.exists()
        assert paths.metadata.exists()
        assert paths.finalize.exists()
        assert paths.transient.exists()
        # transient_movies and transient_series are properties that return paths,
        # but ensure_layout() doesn't create them - they're created on-demand
        assert paths.transient_movies == paths.transient / "Movies"
        assert paths.transient_series == paths.transient / "Series"
    
    def test_raw_directory_computation(self, mock_job, tmp_path):
        """Test that raw directory is computed correctly."""
        jobs_root = tmp_path / "jobs"
        job_dir = jobs_root / mock_job.id
        
        paths = JobPaths(jobs_root, mock_job.id)
        
        expected_raw = job_dir / "raw"
        assert paths.raw == expected_raw
        assert paths.raw.is_relative_to(job_dir)
    
    def test_transient_directories(self, mock_job, tmp_path):
        """Test transient directory structure."""
        jobs_root = tmp_path / "jobs"
        job_dir = jobs_root / mock_job.id
        
        paths = JobPaths(jobs_root, mock_job.id)
        
        assert paths.transient == job_dir / "transient"
        assert paths.transient_movies == job_dir / "transient" / "Movies"
        assert paths.transient_series == job_dir / "transient" / "Series"
    
    def test_paths_with_custom_jobs_root(self, mock_job, tmp_path):
        """Test JobPaths with custom jobs_root."""
        custom_root = tmp_path / "custom" / "jobs"
        job_id = mock_job.id
        
        paths = JobPaths(custom_root, job_id)
        
        assert paths.jobs_root == custom_root
        assert paths.root == custom_root / job_id
        assert paths.raw == custom_root / job_id / "raw"
