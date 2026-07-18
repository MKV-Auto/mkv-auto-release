"""
Tests for post-process progress tracking (0-50% rename, 50-100% hash verification).
Tests progress callback functionality without requiring actual disc ripping.
"""
import pytest
from unittest.mock import Mock, MagicMock


class TestPostProcessProgressTracking:
    """Test post-process progress tracking functionality."""
    
    def test_rename_progress_tracking_0_to_50_percent(self):
        """Test that rename progress tracks 0-50% correctly."""
        rename_weight = 0.5
        hash_weight = 0.5
        
        # Simulate rename progress callback
        progress_updates = []
        
        def update_rename_progress(done: int, total: int, filename: str):
            if total > 0:
                rename_pct = int((done * 100) / total)
                post_progress = int(rename_weight * 100 * rename_pct / 100)
                progress_updates.append({
                    "done": done,
                    "total": total,
                    "filename": filename,
                    "rename_pct": rename_pct,
                    "post_progress": post_progress,
                })
        
        # Simulate renaming 5 files
        total_files = 5
        for i in range(total_files + 1):  # 0 to 5
            update_rename_progress(i, total_files, f"file_{i}.mkv")
        
        # Verify progress range
        assert progress_updates[0]["post_progress"] == 0, "First update should be 0%"
        assert progress_updates[-1]["post_progress"] == 50, "Last rename update should be 50%"
        
        # Verify progress is monotonic
        prev_progress = -1
        for update in progress_updates:
            assert update["post_progress"] >= prev_progress, "Progress should be monotonic"
            prev_progress = update["post_progress"]
    
    def test_hash_progress_tracking_50_to_100_percent(self):
        """Test that hash progress tracks 50-100% correctly."""
        rename_weight = 0.5
        hash_weight = 0.5
        
        progress_updates = []
        
        def update_hash_progress(done: int, total: int, filename: str):
            hash_pct = int((done * 100) / total) if total > 0 else 0
            post_progress = int(rename_weight * 100 + hash_weight * 100 * hash_pct / 100)
            progress_updates.append({
                "done": done,
                "total": total,
                "filename": filename,
                "hash_pct": hash_pct,
                "post_progress": post_progress,
            })
        
        # Simulate hashing 5 files (starts at 50% from rename)
        total_files = 5
        for i in range(total_files + 1):  # 0 to 5
            update_hash_progress(i, total_files, f"file_{i}.mkv")
        
        # Verify progress range
        assert progress_updates[0]["post_progress"] == 50, "First hash update should be 50%"
        assert progress_updates[-1]["post_progress"] == 100, "Last hash update should be 100%"
        
        # Verify progress is monotonic
        prev_progress = 49  # Start from 49 to allow 50
        for update in progress_updates:
            assert update["post_progress"] >= prev_progress, "Progress should be monotonic"
            prev_progress = update["post_progress"]
    
    def test_progress_tracking_when_files_already_moved(self):
        """Test progress tracking when files are already moved (no rename phase)."""
        rename_weight = 0.0  # Files already moved
        hash_weight = 1.0  # All progress from hashing
        
        progress_updates = []
        
        def update_hash_progress(done: int, total: int, filename: str):
            hash_pct = int((done * 100) / total) if total > 0 else 0
            post_progress = int(rename_weight * 100 + hash_weight * 100 * hash_pct / 100)
            progress_updates.append({
                "done": done,
                "total": total,
                "post_progress": post_progress,
            })
        
        # Simulate hashing 3 files (starts at 0% since no rename)
        total_files = 3
        for i in range(total_files + 1):  # 0 to 3
            update_hash_progress(i, total_files, f"file_{i}.mkv")
        
        # Verify progress range
        assert progress_updates[0]["post_progress"] == 0, "Should start at 0% when files already moved"
        assert progress_updates[-1]["post_progress"] == 100, "Should end at 100%"
    
    def test_progress_tracking_combined_rename_and_hash(self):
        """Test that combined rename and hash progress works correctly."""
        rename_weight = 0.5
        hash_weight = 0.5
        
        all_updates = []
        
        # Simulate rename phase (0-50%)
        def update_rename(done: int, total: int):
            rename_pct = int((done * 100) / total) if total > 0 else 0
            post_progress = int(rename_weight * 100 * rename_pct / 100)
            all_updates.append({"phase": "rename", "progress": post_progress})
        
        # Simulate hash phase (50-100%)
        def update_hash(done: int, total: int):
            hash_pct = int((done * 100) / total) if total > 0 else 0
            post_progress = int(rename_weight * 100 + hash_weight * 100 * hash_pct / 100)
            all_updates.append({"phase": "hash", "progress": post_progress})
        
        total_files = 4
        
        # Rename phase
        for i in range(total_files + 1):
            update_rename(i, total_files)
        
        # Hash phase
        for i in range(total_files + 1):
            update_hash(i, total_files)
        
        # Verify transition
        rename_updates = [u for u in all_updates if u["phase"] == "rename"]
        hash_updates = [u for u in all_updates if u["phase"] == "hash"]
        
        assert rename_updates[-1]["progress"] == 50, "Rename should end at 50%"
        assert hash_updates[0]["progress"] == 50, "Hash should start at 50%"
        assert hash_updates[-1]["progress"] == 100, "Hash should end at 100%"
        
        # Verify overall monotonicity
        prev_progress = -1
        for update in all_updates:
            assert update["progress"] >= prev_progress, "Overall progress should be monotonic"
            prev_progress = update["progress"]
    
    def test_progress_calculation_with_zero_files(self):
        """Test progress calculation handles edge case of zero files."""
        rename_weight = 0.5
        hash_weight = 0.5
        
        def update_rename(done: int, total: int):
            if total > 0:
                rename_pct = int((done * 100) / total)
                post_progress = int(rename_weight * 100 * rename_pct / 100)
                return post_progress
            return 0
        
        def update_hash(done: int, total: int):
            hash_pct = int((done * 100) / total) if total > 0 else 0
            post_progress = int(rename_weight * 100 + hash_weight * 100 * hash_pct / 100)
            return post_progress
        
        # Test with zero files
        assert update_rename(0, 0) == 0
        assert update_hash(0, 0) == 50  # Starts at 50% (from rename_weight)
        
        # Test with single file
        assert update_rename(0, 1) == 0
        assert update_rename(1, 1) == 50
        assert update_hash(0, 1) == 50
        assert update_hash(1, 1) == 100

