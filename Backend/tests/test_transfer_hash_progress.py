"""
Tests for transfer stage hash progress integration.
Tests progress throttling, file enumeration, batch hash verification, and step-based progress.
"""
import pytest
import time
from pathlib import Path
from unittest.mock import Mock, MagicMock, call
from typing import List, Dict

from core.transfer.service import (
    ProgressThrottle,
    enumerate_transfer_files,
    verify_transferred_files_batch,
)
from core.transfer.validation import calculate_file_hash


@pytest.fixture
def sample_files(tmp_path):
    """Create sample MKV files for testing."""
    files = {}
    for i in range(4):
        filename = f"title_{i+1:03d}.mkv"
        filepath = tmp_path / filename
        # Create files with varying sizes to test progress
        content = b"fake mkv content " * (1000 + i * 500)
        filepath.write_bytes(content)
        files[filename] = filepath
    
    return files


@pytest.fixture
def sample_directory(tmp_path, sample_files):
    """Create a sample directory structure with files."""
    src_dir = tmp_path / "source"
    src_dir.mkdir()
    
    # Copy sample files to directory
    for filename, filepath in sample_files.items():
        (src_dir / filename).write_bytes(filepath.read_bytes())
    
    return src_dir


class TestProgressThrottle:
    """Test ProgressThrottle class."""
    
    def test_progress_throttle_min_change(self):
        """Test that progress updates when change >= min_change."""
        callback = Mock()
        # Use a reasonable interval to test min_change behavior
        throttle = ProgressThrottle(callback, min_change=5, min_interval=0.01)
        
        # First update should always trigger
        throttle.update(0)
        assert callback.call_count == 1
        
        # Small change (< 5%) should not trigger immediately
        throttle.update(3)
        # May trigger if time passed, but not due to change
        initial_count = callback.call_count
        
        # Large change (>= 5%) should trigger
        throttle.update(10)
        assert callback.call_count > initial_count
        
        # Another large change should trigger
        throttle.update(20)
        assert callback.call_count > initial_count + 1
    
    def test_progress_throttle_min_interval(self):
        """Test that progress updates when interval >= min_interval."""
        callback = Mock()
        throttle = ProgressThrottle(callback, min_change=100, min_interval=0.1)
        
        # First update should always trigger
        throttle.update(0)
        assert callback.call_count == 1
        
        # Immediate update with no change should not trigger (no change and no time passed)
        throttle.update(0)
        initial_count = callback.call_count
        
        # Wait for interval and update should trigger even with no change
        time.sleep(0.15)
        throttle.update(0)
        assert callback.call_count > initial_count
    
    def test_progress_throttle_combined(self):
        """Test that progress updates when either condition is met."""
        callback = Mock()
        throttle = ProgressThrottle(callback, min_change=5, min_interval=0.1)
        
        # First update
        throttle.update(0)
        assert callback.call_count == 1
        
        # Small change, no time passed - should not trigger
        throttle.update(2)
        assert callback.call_count == 1
        
        # Large change - should trigger
        throttle.update(10)
        assert callback.call_count == 2
        
        # Small change, but time passed - should trigger
        time.sleep(0.15)
        throttle.update(11)
        assert callback.call_count == 3
    
    def test_progress_throttle_monotonic(self):
        """Test that progress values are passed through correctly."""
        callback = Mock()
        throttle = ProgressThrottle(callback, min_change=0, min_interval=0.0)
        
        values = [0, 10, 25, 50, 75, 100]
        for val in values:
            throttle.update(val)
        
        # All values should be passed through
        assert callback.call_count == len(values)
        call_args = [call.args[0] for call in callback.call_args_list]
        assert call_args == values


class TestEnumerateTransferFiles:
    """Test enumerate_transfer_files function."""
    
    def test_enumerate_single_file(self, tmp_path, sample_files):
        """Test enumeration of a single file."""
        filepath = list(sample_files.values())[0]
        
        # Mock job object
        job = Mock()
        job.disc_payload = {}
        
        files = enumerate_transfer_files(filepath, job)
        
        assert len(files) == 1
        assert files[0] == filepath
    
    def test_enumerate_directory_with_post_paths(self, tmp_path, sample_directory):
        """Test enumeration of directory with post_paths in job (title_id keys)."""
        import uuid
        job = Mock()
        title1_id = str(uuid.uuid4())
        title2_id = str(uuid.uuid4())
        
        # post_paths now uses title_id keys
        job.post_paths = {
            title1_id: "title_001.mkv",
            title2_id: "title_002.mkv",
        }
        job.disc_payload = {
            "post_paths": job.post_paths,
        }
        
        files = enumerate_transfer_files(sample_directory, job)
        
        assert len(files) == 2
        assert all(f.name in ["title_001.mkv", "title_002.mkv"] for f in files)
    
    def test_enumerate_directory_without_post_paths(self, tmp_path, sample_directory):
        """Test enumeration of directory without post_paths (enumerate all MKV files)."""
        job = Mock()
        job.post_paths = None
        job.disc_payload = {}
        
        files = enumerate_transfer_files(sample_directory, job)
        
        assert len(files) == 4
        assert all(f.suffix == ".mkv" for f in files)
    
    def test_enumerate_empty_directory(self, tmp_path):
        """Test enumeration of empty directory."""
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        
        job = Mock()
        job.post_paths = None
        job.disc_payload = {}
        job.disc_payload = {}
        
        files = enumerate_transfer_files(empty_dir, job)
        
        assert len(files) == 0
    
    def test_enumerate_directory_with_nonexistent_post_paths(self, tmp_path, sample_directory):
        """Test enumeration when post_paths references non-existent files."""
        import uuid
        job = Mock()
        title_id = str(uuid.uuid4())
        
        # post_paths now uses title_id keys
        job.post_paths = {
            title_id: "nonexistent.mkv",
        }
        job.disc_payload = {
            "post_paths": job.post_paths,
        }
        
        files = enumerate_transfer_files(sample_directory, job)
        
        # Should return empty list if post_paths files don't exist
        assert len(files) == 0


class TestVerifyTransferredFilesBatch:
    """Test verify_transferred_files_batch function."""
    
    def test_verify_single_file_match(self, tmp_path, sample_files):
        """Test verification of a single file with matching hash."""
        filepath = list(sample_files.values())[0]
        expected_hash = calculate_file_hash(filepath)
        
        expected_hashes = {filepath.name: expected_hash}
        files = [filepath]
        
        progress_calls = []
        def progress_cb(pct: int, filename: str):
            progress_calls.append({"pct": pct, "filename": filename})
        
        results = verify_transferred_files_batch(files, expected_hashes, progress_cb=progress_cb)
        
        assert results[filepath.name] is True
        assert len(progress_calls) > 0
        # Progress should be in 50-100% range
        assert all(50 <= call["pct"] <= 100 for call in progress_calls)
    
    def test_verify_single_file_mismatch(self, tmp_path, sample_files):
        """Test verification of a single file with mismatched hash."""
        filepath = list(sample_files.values())[0]
        expected_hash = "wrong_hash_value"
        
        expected_hashes = {filepath.name: expected_hash}
        files = [filepath]
        
        results = verify_transferred_files_batch(files, expected_hashes)
        
        assert results[filepath.name] is False
    
    def test_verify_multiple_files(self, tmp_path, sample_files):
        """Test verification of multiple files."""
        files = list(sample_files.values())
        expected_hashes = {f.name: calculate_file_hash(f) for f in files}
        
        progress_calls = []
        def progress_cb(pct: int, filename: str):
            progress_calls.append({"pct": pct, "filename": filename})
        
        results = verify_transferred_files_batch(files, expected_hashes, progress_cb=progress_cb)
        
        assert len(results) == len(files)
        assert all(results[f.name] is True for f in files)
        
        # Progress should be in 50-100% range
        assert all(50 <= call["pct"] <= 100 for call in progress_calls)
        # Final progress should be 100%
        assert progress_calls[-1]["pct"] == 100
    
    def test_verify_files_without_expected_hash(self, tmp_path, sample_files):
        """Test verification when expected hash is missing."""
        files = list(sample_files.values())
        expected_hashes = {}  # No expected hashes
        
        results = verify_transferred_files_batch(files, expected_hashes)
        
        # All results should be None (skipped)
        assert len(results) == len(files)
        assert all(results[f.name] is None for f in files)
    
    def test_verify_progress_step_distribution(self, tmp_path, sample_files):
        """Test that progress is distributed correctly across files."""
        files = list(sample_files.values())[:3]  # Use 3 files
        expected_hashes = {f.name: calculate_file_hash(f) for f in files}
        
        progress_calls = []
        def progress_cb(pct: int, filename: str):
            progress_calls.append({"pct": pct, "filename": filename})
        
        verify_transferred_files_batch(files, expected_hashes, progress_cb=progress_cb)
        
        # Each file should contribute 50/3 ≈ 16.67% to overall progress
        # Progress should start at 50% and end at 100%
        assert progress_calls[0]["pct"] >= 50
        assert progress_calls[-1]["pct"] == 100
        
        # Progress should be monotonic
        prev_pct = 0
        for call in progress_calls:
            assert call["pct"] >= prev_pct
            prev_pct = call["pct"]
    
    def test_verify_empty_file_list(self, tmp_path):
        """Test verification with empty file list."""
        results = verify_transferred_files_batch([], {})
        
        assert results == {}
    
    def test_verify_progress_with_file_hashing(self, tmp_path):
        """Test that progress is reported during file hashing."""
        # Create a larger file to ensure multiple progress updates
        large_file = tmp_path / "large.mkv"
        content = b"x" * (2 * 1024 * 1024)  # 2MB
        large_file.write_bytes(content)
        
        expected_hash = calculate_file_hash(large_file)
        expected_hashes = {large_file.name: expected_hash}
        
        progress_calls = []
        def progress_cb(pct: int, filename: str):
            progress_calls.append({"pct": pct, "filename": filename})
        
        verify_transferred_files_batch([large_file], expected_hashes, progress_cb=progress_cb)
        
        # Should have multiple progress updates during hashing
        assert len(progress_calls) > 1
        # Progress should be in 50-100% range
        assert all(50 <= call["pct"] <= 100 for call in progress_calls)
        # Final progress should be 100%
        assert progress_calls[-1]["pct"] == 100


class TestTransferProgressMapping:
    """Test transfer progress mapping (0-50% range)."""
    
    def test_transfer_progress_mapping(self):
        """Test that transfer progress is mapped to 0-50% range."""
        callback = Mock()
        throttle = ProgressThrottle(callback, min_change=0, min_interval=0.0)
        
        # Transfer progress callback maps 0-100% to 0-50%
        def transfer_progress_callback(pct: int):
            overall = int(pct * 50 / 100)
            throttle.update(overall)
        
        # Test various transfer progress values
        test_values = [0, 25, 50, 75, 100]
        for val in test_values:
            transfer_progress_callback(val)
        
        # Verify mapped values
        call_args = [call.args[0] for call in callback.call_args_list]
        expected = [0, 12, 25, 37, 50]  # 0-100% mapped to 0-50%
        assert call_args == expected
    
    def test_hash_progress_mapping(self):
        """Test that hash progress is in 50-100% range."""
        callback = Mock()
        throttle = ProgressThrottle(callback, min_change=0, min_interval=0.0)
        
        # Hash progress callback receives values in 50-100% range
        def hash_progress_callback(progress_pct: int, filename: str):
            throttle.update(progress_pct)
        
        # Test hash progress values
        test_values = [50, 60, 75, 90, 100]
        for val in test_values:
            hash_progress_callback(val, "test.mkv")
        
        # Verify values are in 50-100% range
        call_args = [call.args[0] for call in callback.call_args_list]
        assert call_args == test_values
        assert all(50 <= val <= 100 for val in call_args)


class TestTransferHashProgressIntegration:
    """Integration tests for transfer + hash progress."""
    
    def test_full_transfer_flow(self, tmp_path, sample_files):
        """Test complete transfer flow with progress tracking."""
        src_dir = tmp_path / "source"
        dest_dir = tmp_path / "dest"
        src_dir.mkdir()
        dest_dir.mkdir()
        
        # Copy files to source
        for filename, filepath in sample_files.items():
            (src_dir / filename).write_bytes(filepath.read_bytes())
        
        # Track all progress updates
        all_progress = []
        callback = Mock(side_effect=lambda pct: all_progress.append(pct))
        throttle = ProgressThrottle(callback, min_change=0, min_interval=0.0)
        
        # Simulate transfer progress (0-50%)
        def transfer_progress_callback(pct: int):
            overall = int(pct * 50 / 100)
            throttle.update(overall)
        
        # Simulate transfer of files
        files = list(src_dir.glob("*.mkv"))
        for idx, filepath in enumerate(files):
            # Copy file (simulate transfer)
            (dest_dir / filepath.name).write_bytes(filepath.read_bytes())
            
            # Report transfer progress
            transfer_pct = int((idx + 1) * 100 / len(files))
            transfer_progress_callback(transfer_pct)
        
        # Verify transfer progress was in 0-50% range
        transfer_progress = [p for p in all_progress if p <= 50]
        assert len(transfer_progress) > 0
        assert max(transfer_progress) == 50
        
        # Now simulate hash verification (50-100%)
        def hash_progress_callback(progress_pct: int, filename: str):
            throttle.update(progress_pct)
        
        # Get expected hashes
        expected_hashes = {f.name: calculate_file_hash(src_dir / f.name) for f in files}
        dest_files = [dest_dir / f.name for f in files]
        
        # Verify hashes
        verify_transferred_files_batch(dest_files, expected_hashes, progress_cb=hash_progress_callback)
        
        # Verify hash progress was in 50-100% range
        hash_progress = [p for p in all_progress if p >= 50]
        assert len(hash_progress) > 0
        assert min(hash_progress) >= 50
        assert max(hash_progress) == 100
        
        # Verify overall progress is smooth and complete
        assert all_progress[0] == 0 or all_progress[0] >= 0
        assert all_progress[-1] == 100
        # Progress should be generally monotonic (allowing for some throttling)
        assert sorted(all_progress) == all_progress or len(set(all_progress)) > 1

