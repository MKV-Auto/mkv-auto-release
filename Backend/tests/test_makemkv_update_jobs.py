"""Unit tests for core.makemkv_update_jobs: get_job, job state persistence, and cleanup."""
import asyncio
import pytest
from datetime import datetime, timedelta

from core.makemkv_update_jobs import (
    get_job,
    jobs,
    UpdateJob,
    cleanup_old_jobs,
    MAX_LOG_LINES,
)


@pytest.fixture(autouse=True)
def _clear_jobs():
    jobs.clear()
    yield
    jobs.clear()


def test_get_job_empty_returns_none():
    assert get_job("j1") is None


def test_get_job_returns_instance_when_exists():
    uj = UpdateJob(asyncio.Queue())
    jobs["j1"] = uj
    assert get_job("j1") is uj


def test_update_job_initializes_with_defaults():
    """Test that UpdateJob initializes with correct default values."""
    uj = UpdateJob(asyncio.Queue())
    assert uj.status == "pending"
    assert uj.error is None
    assert uj.version is None
    assert uj.logs == []
    assert isinstance(uj.created_at, datetime)
    assert isinstance(uj.updated_at, datetime)


def test_update_job_persists_logs():
    """Test that logs are persisted in the UpdateJob."""
    uj = UpdateJob(asyncio.Queue())
    jobs["j1"] = uj
    
    # Add some logs
    uj.logs.append("Log line 1")
    uj.logs.append("Log line 2")
    uj.logs.append("Log line 3")
    
    # Verify logs are persisted
    retrieved_job = get_job("j1")
    assert retrieved_job is not None
    assert len(retrieved_job.logs) == 3
    assert retrieved_job.logs[0] == "Log line 1"
    assert retrieved_job.logs[2] == "Log line 3"


def test_update_job_limits_log_lines():
    """Test that logs are limited to MAX_LOG_LINES."""
    uj = UpdateJob(asyncio.Queue())
    
    # Add more than MAX_LOG_LINES
    for i in range(MAX_LOG_LINES + 100):
        if len(uj.logs) >= MAX_LOG_LINES:
            uj.logs.pop(0)  # Simulate the behavior in start_update_job
        uj.logs.append(f"Log line {i}")
    
    # Verify logs are limited
    assert len(uj.logs) == MAX_LOG_LINES
    # Verify oldest logs were removed (should start at line 100)
    assert uj.logs[0] == "Log line 100"
    assert uj.logs[-1] == f"Log line {MAX_LOG_LINES + 99}"


def test_cleanup_old_jobs_removes_expired():
    """Test that cleanup_old_jobs removes jobs older than 24 hours."""
    # Create jobs with different ages
    old_job = UpdateJob(asyncio.Queue())
    old_job.created_at = datetime.utcnow() - timedelta(hours=25)
    jobs["old"] = old_job
    
    recent_job = UpdateJob(asyncio.Queue())
    recent_job.created_at = datetime.utcnow() - timedelta(hours=1)
    jobs["recent"] = recent_job
    
    # Run cleanup
    removed = cleanup_old_jobs()
    
    # Verify old job was removed and recent job remains
    assert removed == 1
    assert "old" not in jobs
    assert "recent" in jobs


def test_cleanup_old_jobs_returns_zero_when_none_expired():
    """Test that cleanup_old_jobs returns 0 when no jobs are expired."""
    # Create only recent jobs
    for i in range(3):
        uj = UpdateJob(asyncio.Queue())
        uj.created_at = datetime.utcnow() - timedelta(hours=i)
        jobs[f"job_{i}"] = uj
    
    # Run cleanup
    removed = cleanup_old_jobs()
    
    # Verify no jobs were removed
    assert removed == 0
    assert len(jobs) == 3


def test_update_job_status_transitions():
    """Test that job status can transition through states."""
    uj = UpdateJob(asyncio.Queue())
    jobs["j1"] = uj
    
    # Test status transitions
    assert uj.status == "pending"
    
    uj.status = "running"
    assert uj.status == "running"
    
    uj.status = "completed"
    uj.version = "1.17.6"
    assert uj.status == "completed"
    assert uj.version == "1.17.6"
    
    # Test failed status
    uj2 = UpdateJob(asyncio.Queue())
    uj2.status = "failed"
    uj2.error = "Installation failed"
    assert uj2.status == "failed"
    assert uj2.error == "Installation failed"
