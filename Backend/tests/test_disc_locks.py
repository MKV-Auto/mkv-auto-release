"""
Tests for unified locking system for disc operations.
"""
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock

import pytest
from filelock import FileLock, Timeout

from core.disc_locks import (
    get_operation_lock_path,
    is_operation_active,
    get_active_operations,
    get_disc_lock_debug_snapshot,
    acquire_operation_lock,
    release_operation_lock,
    OPERATION_HASH,
    OPERATION_INFO,
    OPERATION_RIP,
)


@pytest.fixture
def tmp_lock_dir(tmp_path, monkeypatch):
    """Override lock directory to use tmp."""
    lock_dir = tmp_path / "disc_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    
    def mock_get_mkvauto_tmp():
        return tmp_path
    
    monkeypatch.setattr("core.disc_locks.get_mkvauto_tmp", mock_get_mkvauto_tmp)
    
    return lock_dir


class TestGetOperationLockPath:
    """Tests for get_operation_lock_path function."""
    
    def test_get_operation_lock_path(self, tmp_lock_dir):
        """Test getting lock path for operation."""
        path = get_operation_lock_path("1", OPERATION_RIP)
        
        assert path.parent == tmp_lock_dir
        assert path.name == "1.rip.lock"
    
    def test_get_operation_lock_path_different_operations(self, tmp_lock_dir):
        """Test getting lock paths for different operations."""
        hash_path = get_operation_lock_path("1", OPERATION_HASH)
        info_path = get_operation_lock_path("1", OPERATION_INFO)
        rip_path = get_operation_lock_path("1", OPERATION_RIP)
        
        assert hash_path.name == "1.hash.lock"
        assert info_path.name == "1.info.lock"
        assert rip_path.name == "1.rip.lock"


class TestIsOperationActive:
    """Tests for is_operation_active function."""
    
    def test_is_operation_active_no_lock(self, tmp_lock_dir):
        """Test operation active check when no lock exists."""
        result = is_operation_active("1", OPERATION_RIP)
        assert result is False
    
    def test_is_operation_active_with_lock(self, tmp_lock_dir):
        """Test operation active check when lock is held."""
        lock_path = get_operation_lock_path("1", OPERATION_RIP)
        lock = FileLock(lock_path)
        lock.acquire()
        
        try:
            result = is_operation_active("1", OPERATION_RIP)
            assert result is True
        finally:
            lock.release()
    
    def test_is_operation_active_stale_lock(self, tmp_lock_dir):
        """Test operation active check with stale lock file."""
        lock_path = get_operation_lock_path("1", OPERATION_RIP)
        # Create lock file but don't hold it
        lock_path.touch()
        
        # Should detect stale lock and return False
        result = is_operation_active("1", OPERATION_RIP)
        assert result is False
        # Stale lock should be removed
        assert not lock_path.exists()
    
    @patch("core.disc_locks._is_makemkvcon_running_for_operation")
    def test_is_operation_active_with_process(self, mock_check_process, tmp_lock_dir):
        """Test operation active check when process is running."""
        mock_check_process.return_value = True

        result = is_operation_active("1", OPERATION_RIP)
        assert result is True
        mock_check_process.assert_called_with(OPERATION_RIP, mount_point=None, disc_num="1")


class TestGetActiveOperations:
    """Tests for get_active_operations function."""
    
    def test_get_active_operations_none(self, tmp_lock_dir):
        """Test getting active operations when none are active."""
        result = get_active_operations("1")
        assert result == []
    
    def test_get_active_operations_single(self, tmp_lock_dir):
        """Test getting active operations with one active."""
        lock_path = get_operation_lock_path("1", OPERATION_RIP)
        lock = FileLock(lock_path)
        lock.acquire()
        
        try:
            result = get_active_operations("1")
            assert OPERATION_RIP in result
        finally:
            lock.release()
    
    def test_get_active_operations_multiple(self, tmp_lock_dir):
        """Test getting active operations with multiple active."""
        hash_lock = FileLock(get_operation_lock_path("1", OPERATION_HASH))
        info_lock = FileLock(get_operation_lock_path("1", OPERATION_INFO))
        
        hash_lock.acquire()
        info_lock.acquire()
        
        try:
            result = get_active_operations("1")
            assert OPERATION_HASH in result
            assert OPERATION_INFO in result
        finally:
            hash_lock.release()
            info_lock.release()


class TestAcquireOperationLock:
    """Tests for acquire_operation_lock function."""
    
    def test_acquire_operation_lock_success(self, tmp_lock_dir):
        """Test acquiring operation lock successfully."""
        lock = acquire_operation_lock("1", OPERATION_RIP)
        
        assert lock is not None
        assert isinstance(lock, FileLock)
        
        # Verify lock file exists
        lock_path = get_operation_lock_path("1", OPERATION_RIP)
        assert lock_path.exists()
        
        # Clean up
        release_operation_lock(lock)
    
    def test_acquire_operation_lock_when_active(self, tmp_lock_dir):
        """Test acquiring lock when operation is already active."""
        # Acquire lock first
        first_lock = acquire_operation_lock("1", OPERATION_RIP)
        assert first_lock is not None
        
        # Try to acquire again (should fail)
        second_lock = acquire_operation_lock("1", OPERATION_RIP, timeout=0.1)
        assert second_lock is None
        
        # Clean up
        release_operation_lock(first_lock)
    
    @patch("core.disc_locks.is_operation_active")
    def test_acquire_operation_lock_checks_active(self, mock_is_active, tmp_lock_dir):
        """Test acquiring lock checks if operation is active."""
        mock_is_active.return_value = True

        lock = acquire_operation_lock("1", OPERATION_RIP)
        assert lock is None
        mock_is_active.assert_called_with("1", OPERATION_RIP, mount_point=None)


class TestReleaseOperationLock:
    """Tests for release_operation_lock function."""
    
    def test_release_operation_lock(self, tmp_lock_dir):
        """Test releasing operation lock."""
        lock = acquire_operation_lock("1", OPERATION_RIP)
        assert lock is not None
        
        release_operation_lock(lock)
        
        # Lock should be released (can acquire again)
        new_lock = acquire_operation_lock("1", OPERATION_RIP)
        assert new_lock is not None
        release_operation_lock(new_lock)
    
    def test_release_operation_lock_none(self, tmp_lock_dir):
        """Test releasing None lock (should not error)."""
        release_operation_lock(None)
        # Should not raise


class TestIsMakemkvconRunningForOperation:
    """Tests for _is_makemkvcon_running_for_operation function."""
    
    @patch("core.disc_locks.psutil.process_iter")
    def test_is_makemkvcon_running_for_rip_with_psutil(self, mock_process_iter, tmp_lock_dir):
        """Test process detection for rip operation with psutil."""
        # Mock process with rip command
        mock_proc = Mock()
        mock_proc.cmdline.return_value = ["makemkvcon", "mkv", "disc:1", "all", "/tmp"]
        
        mock_process_iter.return_value = [mock_proc]
        
        from core.disc_locks import _is_makemkvcon_running_for_operation
        
        result = _is_makemkvcon_running_for_operation(OPERATION_RIP, disc_num="1")
        assert result is True
    
    @patch("core.disc_locks.psutil.process_iter")
    def test_is_makemkvcon_running_for_info_with_psutil(self, mock_process_iter, tmp_lock_dir):
        """Test process detection for info operation with psutil."""
        # Mock process with info command
        mock_proc = Mock()
        mock_proc.cmdline.return_value = ["makemkvcon", "info", "disc:1"]
        
        mock_process_iter.return_value = [mock_proc]
        
        from core.disc_locks import _is_makemkvcon_running_for_operation
        
        result = _is_makemkvcon_running_for_operation(OPERATION_INFO, disc_num="1")
        assert result is True
    
    @patch("core.disc_locks.psutil.process_iter", side_effect=ImportError())
    @patch("subprocess.run")
    def test_is_makemkvcon_running_for_rip_with_pgrep(self, mock_subprocess, mock_process_iter, tmp_lock_dir):
        """Test process detection for rip operation with pgrep fallback."""
        # Mock pgrep success
        mock_result = Mock()
        mock_result.returncode = 0
        mock_subprocess.return_value = mock_result
        
        from core.disc_locks import _is_makemkvcon_running_for_operation
        
        result = _is_makemkvcon_running_for_operation(OPERATION_RIP, disc_num="1")
        assert result is True
        mock_subprocess.assert_called()
    
    @patch("core.disc_locks.psutil.process_iter")
    def test_is_makemkvcon_running_wrong_disc(self, mock_process_iter, tmp_lock_dir):
        """Test process detection ignores processes for different disc."""
        # Mock process for different disc
        mock_proc = Mock()
        mock_proc.cmdline.return_value = ["makemkvcon", "mkv", "disc:2", "all", "/tmp"]
        
        mock_process_iter.return_value = [mock_proc]
        
        from core.disc_locks import _is_makemkvcon_running_for_operation
        
        result = _is_makemkvcon_running_for_operation(OPERATION_RIP, disc_num="1")
        assert result is False
    
    @patch("core.disc_locks.psutil.process_iter")
    def test_is_makemkvcon_running_no_processes(self, mock_process_iter, tmp_lock_dir):
        """Test process detection when no processes running."""
        mock_process_iter.return_value = []
        
        from core.disc_locks import _is_makemkvcon_running_for_operation
        
        result = _is_makemkvcon_running_for_operation(OPERATION_RIP, disc_num="1")
        assert result is False


class TestDiscLockDebugSnapshot:
    """Diagnostics for drive-busy debugging."""

    def test_snapshot_shape(self, tmp_lock_dir):
        snap = get_disc_lock_debug_snapshot("424242")
        assert snap["key"] == "424242"
        assert "active_operations" in snap
        assert "lock_files" in snap
        assert OPERATION_RIP in snap["lock_files"]
        assert "held" in snap["lock_files"][OPERATION_RIP]
        assert "duplicate_rip_suspected" in snap
        assert "rip_lock_file_held" in snap

    def test_snapshot_shows_held_rip_lock(self, tmp_lock_dir):
        p = get_operation_lock_path("7", OPERATION_RIP)
        lock = FileLock(p)
        lock.acquire()
        try:
            snap = get_disc_lock_debug_snapshot("7")
            assert snap["lock_files"][OPERATION_RIP]["held"] is True
            assert snap["duplicate_rip_suspected"] is True
        finally:
            lock.release()

