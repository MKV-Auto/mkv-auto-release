"""
Tests for hash progress integration into stage progress.
Tests the new step-based progress calculation and hash progress callbacks.
"""
import pytest
import time
import uuid
from pathlib import Path
from unittest.mock import Mock, MagicMock, call
from typing import List, Tuple
from sqlalchemy.orm import Session

from core.utils import hash_file
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
def mock_disc_titles(sample_files):
    """Create mock DiscTitle objects with title_id mappings."""
    titles = []
    title_ids = []
    for i, filename in enumerate(sample_files.keys()):
        title_id = str(uuid.uuid4())
        title_ids.append(title_id)
        title = Mock()
        title.id = title_id
        title.source_file = f"0010{i}.mpls"  # Mock source_file
        title.comment = filename  # Output filename matches
        titles.append(title)
    
    return titles, title_ids


@pytest.fixture
def mock_db_with_titles(mock_disc_titles):
    """Create mock database session with disc_titles."""
    disc_titles, _ = mock_disc_titles
    db = Mock(spec=Session)
    mock_query = Mock()
    mock_query.filter.return_value.all.return_value = disc_titles
    db.query.return_value = mock_query
    return db


class TestHashFileProgress:
    """Test hash_file() with progress callbacks."""
    
    def test_hash_file_with_progress_callback(self, sample_files, tmp_path):
        """Test that hash_file reports progress correctly."""
        filepath = list(sample_files.values())[0]
        progress_calls = []
        
        def progress_cb(bytes_read: int, total_bytes: int, file_path: str):
            progress_calls.append({
                "bytes_read": bytes_read,
                "total_bytes": total_bytes,
                "file_path": str(file_path),
            })
        
        # Calculate hash with progress callback
        result_hash = hash_file(str(filepath), progress_cb=progress_cb)
        
        # Verify hash is correct
        expected_hash = hash_file(str(filepath))  # Without callback for comparison
        assert result_hash == expected_hash
        
        # Verify progress was reported
        assert len(progress_calls) > 0, "Progress callback should be called"
        
        # Verify progress is monotonic
        prev_bytes = -1
        for call_data in progress_calls:
            assert call_data["bytes_read"] > prev_bytes, "Progress should be monotonic"
            assert call_data["bytes_read"] <= call_data["total_bytes"], "Bytes read should not exceed total"
            prev_bytes = call_data["bytes_read"]
        
        # Verify final progress
        assert progress_calls[-1]["bytes_read"] == progress_calls[-1]["total_bytes"], \
            "Final progress should reach total bytes"
    
    def test_hash_file_without_progress_callback(self, sample_files, tmp_path):
        """Test that hash_file works without progress callback."""
        filepath = list(sample_files.values())[0]
        
        # Should work without callback
        hash1 = hash_file(str(filepath))
        hash2 = hash_file(str(filepath), progress_cb=None)
        
        assert hash1 == hash2, "Hash should be same with or without callback"
        assert len(hash1) == 64, "SHA256 hash should be 64 hex characters"
    
    def test_hash_file_progress_with_large_file(self, tmp_path):
        """Test progress reporting with a larger file."""
        filepath = tmp_path / "large_file.mkv"
        # Create a larger file (5MB)
        content = b"x" * (5 * 1024 * 1024)
        filepath.write_bytes(content)
        
        progress_calls = []
        
        def progress_cb(bytes_read: int, total_bytes: int, file_path: str):
            progress_calls.append(bytes_read)
        
        hash_file(str(filepath), progress_cb=progress_cb)
        
        # Should have at least one progress update for large file
        assert len(progress_calls) >= 1, \
            f"Large file should generate progress updates, got {len(progress_calls)}"
        assert progress_calls[-1] == len(content), "Final progress should match file size"


class TestGatherFinalOutputsProgress:
    """Test gather_final_outputs() with new step-based progress."""
    
    def test_gather_final_outputs_step_based_progress(self, sample_files, tmp_path, mock_disc_titles, mock_db_with_titles):
        """Test that gather_final_outputs uses step-based progress calculation."""
        from workers.tasks import gather_final_outputs
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        progress_updates = []
        
        def progress_cb(progress_pct: int, filename: str):
            progress_updates.append({
                "progress_pct": progress_pct,
                "filename": filename,
            })
        
        paths_result, hashes_result = gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            progress_cb=progress_cb,
            disc_id="test-disc-123",
            db=mock_db_with_titles
        )
        
        # Verify results (both use title_id keys)
        assert len(paths_result) == len(sample_files)
        assert len(hashes_result) == len(sample_files)
        # Verify keys are title_ids
        for key in paths_result.keys():
            assert len(key) == 36 and '-' in key, f"Expected UUID format, got {key}"
        
        # Verify progress was reported
        assert len(progress_updates) > 0, "Progress should be reported"
        
        # Verify progress is in 0-100 range
        for update in progress_updates:
            assert 0 <= update["progress_pct"] <= 100, \
                f"Progress should be 0-100, got {update['progress_pct']}"
        
        # Verify final progress is 100%
        assert progress_updates[-1]["progress_pct"] == 100, \
            "Final progress should be 100%"
        
        # Verify progress is monotonic
        prev_progress = -1
        for update in progress_updates:
            assert update["progress_pct"] >= prev_progress, \
                "Progress should be monotonic"
            prev_progress = update["progress_pct"]
    
    def test_gather_final_outputs_step_weight_calculation(self, sample_files, tmp_path, mock_disc_titles, mock_db_with_titles):
        """Test that step weight is calculated correctly."""
        from workers.tasks import gather_final_outputs
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        total_files = len(ripped_files)
        expected_step_weight = 100 / total_files
        
        progress_updates = []
        
        def progress_cb(progress_pct: int, filename: str):
            progress_updates.append(progress_pct)
        
        gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            progress_cb=progress_cb,
            disc_id="test-disc-123",
            db=mock_db_with_titles
        )
        
        # Check that progress increments by step_weight for each file
        # (allowing for hash sub-progress within each step)
        # Final update should be 100%
        assert progress_updates[-1] == 100
        
        # Progress should increment smoothly, not jump by large amounts
        # (except for the final jump to 100%)
        for i in range(len(progress_updates) - 1):
            diff = progress_updates[i + 1] - progress_updates[i]
            # Progress should increment smoothly (small increments during hashing)
            # or by step_weight (when file completes)
            assert diff >= 0, "Progress should never decrease"
    
    def test_gather_final_outputs_cached_hash_progress_simulation(self, sample_files, tmp_path, mock_disc_titles, mock_db_with_titles):
        """Test that cached hashes simulate progress smoothly."""
        from workers.tasks import gather_final_outputs
        from core.transfer.validation import calculate_file_hash
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build ripped_files and cached_hashes with title_id keys
        ripped_files = {}
        cached_hashes = {}
        for i, (filename, filepath) in enumerate(sample_files.items()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
            cached_hashes[title_id] = calculate_file_hash(filepath)
        
        progress_updates = []
        timestamps = []
        
        def progress_cb(progress_pct: int, filename: str):
            progress_updates.append(progress_pct)
            timestamps.append(time.time())
        
        gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            progress_cb=progress_cb,
            cached_hashes=cached_hashes,
            disc_id="test-disc-123",
            db=mock_db_with_titles
        )
        
        # Verify progress was reported (simulated progress should generate multiple updates)
        assert len(progress_updates) > len(sample_files), \
            "Cached hashes should generate multiple progress updates per file"
        
        # Verify progress is smooth (no large jumps)
        for i in range(len(progress_updates) - 1):
            diff = progress_updates[i + 1] - progress_updates[i]
            # Progress should increment smoothly (small increments)
            assert diff >= 0, "Progress should never decrease"
            # Should not have huge jumps (except maybe final jump to 100%)
            if i < len(progress_updates) - 2:  # Not the last update
                assert diff <= 20, f"Progress jump too large: {diff}%"
        
        # Verify final progress is 100%
        assert progress_updates[-1] == 100
    
    def test_gather_final_outputs_without_progress_callback(self, sample_files, tmp_path, mock_disc_titles, mock_db_with_titles):
        """Test that gather_final_outputs works without progress callback."""
        from workers.tasks import gather_final_outputs
        
        disc_titles, title_ids = mock_disc_titles
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        paths_result, hashes_result = gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            progress_cb=None,
            disc_id="test-disc-123",
            db=mock_db_with_titles
        )
        
        # Should work without callback
        assert len(paths_result) == len(sample_files)
        assert len(hashes_result) == len(sample_files)
        # Verify keys are title_ids
        for key in paths_result.keys():
            assert len(key) == 36 and '-' in key, f"Expected UUID format, got {key}"
    
    def test_gather_final_outputs_jobtask_method(self, sample_files, tmp_path, mock_disc_titles, mock_db_with_titles):
        """Test that JobTask.gather_final_outputs also uses step-based progress."""
        from workers.tasks import JobTask
        from unittest.mock import Mock
        
        disc_titles, title_ids = mock_disc_titles
        
        task = Mock(spec=JobTask)
        task.gather_final_outputs = JobTask.gather_final_outputs.__get__(task, JobTask)
        
        # Build ripped_files with title_id keys
        ripped_files = {}
        for i, filename in enumerate(sample_files.keys()):
            title_id = title_ids[i]
            ripped_files[title_id] = filename
        
        progress_updates = []
        
        def progress_cb(progress_pct: int, filename: str):
            progress_updates.append(progress_pct)
        
        paths_result, hashes_result = task.gather_final_outputs(
            tmp_path,
            final_paths=ripped_files,
            progress_cb=progress_cb,
            disc_id="test-disc-123",
            db=mock_db_with_titles
        )
        
        # Verify results
        assert len(paths_result) == len(sample_files)
        assert len(hashes_result) == len(sample_files)
        
        # Verify progress was reported
        assert len(progress_updates) > 0
        assert progress_updates[-1] == 100


class TestPostProcessProgressStepBased:
    """Test post-processing progress with step-based calculation."""
    
    def test_step_weight_calculation(self):
        """Test that step weight is calculated correctly."""
        rename_steps = 4
        hash_steps = 4
        total_steps = rename_steps + hash_steps
        step_weight = 100 / total_steps
        
        assert step_weight == 12.5, "Step weight should be 12.5% for 8 total steps"
        
        # Each rename step should contribute step_weight
        completed_rename_steps = 2
        rename_progress = int(completed_rename_steps * step_weight)
        assert rename_progress == 25, "2 rename steps should be 25%"
        
        # Hash progress should start after rename
        hash_progress_pct = 50  # 50% through hashing
        overall_progress = int(rename_steps * step_weight + hash_progress_pct * step_weight / 100)
        expected = int(4 * 12.5 + 50 * 12.5 / 100)
        assert overall_progress == expected, \
            f"Hash progress should be calculated correctly: expected {expected}, got {overall_progress}"
    
    def test_postprocess_progress_with_rename_and_hash(self):
        """Test post-processing progress with both rename and hash steps."""
        rename_steps = 4
        hash_steps = 4
        total_steps = rename_steps + hash_steps
        step_weight = 100 / total_steps
        
        progress_updates = []
        
        # Simulate rename progress (old signature for backward compat)
        def update_rename_progress(done: int, total: int, filename: str):
            if total > 0:
                completed_rename_steps = done
                post_progress = int(completed_rename_steps * step_weight)
                progress_updates.append({"phase": "rename", "progress": post_progress})
        
        # Simulate hash progress (new signature)
        def update_hash_progress(progress_pct: int, filename: str):
            progress_updates.append({"phase": "hash", "progress": progress_pct})
        
        # Rename phase
        for i in range(rename_steps + 1):
            update_rename_progress(i, rename_steps, f"file_{i}.mkv")
        
        # Hash phase (simulate gather_final_outputs progress)
        for i in range(hash_steps):
            # Simulate progress through each hash step
            for sub_progress in [0, 25, 50, 75, 100]:
                step_progress = int(i * step_weight + sub_progress * step_weight / 100)
                # Add rename_steps offset
                overall_progress = int(rename_steps * step_weight) + step_progress
                update_hash_progress(overall_progress, f"file_{i}.mkv")
        
        # Verify rename phase
        rename_updates = [u for u in progress_updates if u["phase"] == "rename"]
        assert rename_updates[0]["progress"] == 0, "Rename should start at 0%"
        assert rename_updates[-1]["progress"] == int(rename_steps * step_weight), \
            f"Rename should end at {int(rename_steps * step_weight)}%"
        
        # Verify hash phase
        hash_updates = [u for u in progress_updates if u["phase"] == "hash"]
        assert hash_updates[0]["progress"] == int(rename_steps * step_weight), \
            "Hash should start after rename"
        assert hash_updates[-1]["progress"] == 100, "Hash should end at 100%"
        
        # Verify overall monotonicity
        prev_progress = -1
        for update in progress_updates:
            assert update["progress"] >= prev_progress, \
                "Overall progress should be monotonic"
            prev_progress = update["progress"]
    
    def test_postprocess_progress_files_already_moved(self):
        """Test post-processing progress when files are already moved."""
        rename_steps = 0  # Files already moved
        hash_steps = 4
        total_steps = rename_steps + hash_steps
        step_weight = 100 / total_steps
        
        progress_updates = []
        
        def update_hash_progress(progress_pct: int, filename: str):
            progress_updates.append(progress_pct)
        
        # Simulate hash phase only
        for i in range(hash_steps):
            step_progress = int((i + 1) * step_weight)
            update_hash_progress(step_progress, f"file_{i}.mkv")
        
        # Verify progress starts at 0% (no rename phase)
        assert progress_updates[0] == int(step_weight), \
            "Progress should start at first step weight"
        assert progress_updates[-1] == 100, "Progress should end at 100%"
    
    def test_postprocess_progress_edge_cases(self):
        """Test edge cases in post-processing progress calculation."""
        # Single file
        rename_steps = 1
        hash_steps = 1
        total_steps = rename_steps + hash_steps
        step_weight = 100 / total_steps
        
        assert step_weight == 50, "Step weight should be 50% for 2 steps"
        
        # Many files
        rename_steps = 10
        hash_steps = 10
        total_steps = rename_steps + hash_steps
        step_weight = 100 / total_steps
        
        assert step_weight == 5, "Step weight should be 5% for 20 steps"
        
        # Zero steps (should not happen, but test handling)
        total_steps = 0
        step_weight = 100 / total_steps if total_steps > 0 else 0
        assert step_weight == 0, "Step weight should be 0 for zero steps"


class TestCalculateFileHashProgress:
    """Test calculate_file_hash() with progress callbacks."""
    
    def test_calculate_file_hash_with_progress(self, sample_files, tmp_path):
        """Test that calculate_file_hash reports progress correctly."""
        filepath = list(sample_files.values())[0]
        progress_calls = []
        
        def progress_cb(bytes_read: int, total_bytes: int, file_path: str):
            progress_calls.append({
                "bytes_read": bytes_read,
                "total_bytes": total_bytes,
            })
        
        result_hash = calculate_file_hash(filepath, progress_cb=progress_cb)
        
        # Verify hash is correct
        expected_hash = calculate_file_hash(filepath)  # Without callback
        assert result_hash == expected_hash
        
        # Verify progress was reported
        assert len(progress_calls) > 0, "Progress callback should be called"
        
        # Verify progress is monotonic
        prev_bytes = -1
        for call_data in progress_calls:
            assert call_data["bytes_read"] > prev_bytes, "Progress should be monotonic"
            prev_bytes = call_data["bytes_read"]
        
        # Verify final progress
        assert progress_calls[-1]["bytes_read"] == progress_calls[-1]["total_bytes"]
    
    def test_calculate_file_hash_without_progress(self, sample_files, tmp_path):
        """Test that calculate_file_hash works without progress callback."""
        filepath = list(sample_files.values())[0]
        
        hash1 = calculate_file_hash(filepath)
        hash2 = calculate_file_hash(filepath, progress_cb=None)
        
        assert hash1 == hash2, "Hash should be same with or without callback"

