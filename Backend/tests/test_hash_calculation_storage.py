"""
Tests for hash calculation and storage at end of rip stage.
Tests the hash calculation logic without requiring actual disc ripping.
"""
import pytest
import json
from pathlib import Path
from unittest.mock import Mock, patch
from sqlalchemy.orm import Session

from core.transfer.validation import calculate_file_hash
from core.job_paths import JobPaths


@pytest.fixture
def mock_rip_environment(tmp_path):
    """Set up a mock rip environment with files."""
    job_id = "test-job-hash"
    paths = JobPaths(tmp_path, job_id)
    paths.ensure_layout()
    
    # Create mock MKV files in raw/
    raw_dir = paths.raw
    files = {}
    for i in range(3):
        filename = f"title_{i+1:03d}.mkv"
        filepath = raw_dir / filename
        # Create files with different content
        content = b"fake mkv content " * (100 + i * 50)
        filepath.write_bytes(content)
        files[filename] = filepath
    
    return {
        "job_id": job_id,
        "paths": paths,
        "raw_dir": raw_dir,
        "files": files,
    }


class TestHashCalculation:
    """Test hash calculation functionality."""
    
    def test_hash_calculation_consistency(self, mock_rip_environment):
        """Test that hash calculation is consistent."""
        files = mock_rip_environment["files"]
        filepath = list(files.values())[0]
        
        hash1 = calculate_file_hash(filepath)
        hash2 = calculate_file_hash(filepath)
        
        assert hash1 == hash2, "Hash should be consistent for same file"
        assert len(hash1) == 64, "SHA256 hash should be 64 hex characters"
    
    def test_hash_calculation_different_files(self, mock_rip_environment):
        """Test that different files produce different hashes."""
        files = mock_rip_environment["files"]
        file_list = list(files.values())
        
        hash1 = calculate_file_hash(file_list[0])
        hash2 = calculate_file_hash(file_list[1])
        hash3 = calculate_file_hash(file_list[2])
        
        assert hash1 != hash2, "Different files should have different hashes"
        assert hash2 != hash3, "Different files should have different hashes"
        assert hash1 != hash3, "Different files should have different hashes"
    
    def test_hash_calculation_file_content_change(self, mock_rip_environment):
        """Test that hash changes when file content changes."""
        files = mock_rip_environment["files"]
        filepath = list(files.values())[0]
        
        original_hash = calculate_file_hash(filepath)
        
        # Modify file content
        filepath.write_bytes(b"modified content" * 100)
        modified_hash = calculate_file_hash(filepath)
        
        assert original_hash != modified_hash, "Hash should change when content changes"


class TestHashStorage:
    """Test hash storage in disc_payload."""
    
    def test_hash_storage_structure(self, mock_rip_environment):
        """Test the structure of hash storage in disc_payload."""
        files = mock_rip_environment["files"]
        raw_dir = mock_rip_environment["raw_dir"]
        
        # Simulate hash calculation and storage
        source_hashes = {}
        source_files = {}
        
        for filename, filepath in files.items():
            source_hashes[filename] = calculate_file_hash(filepath)
            source_files[filename] = str(filepath.relative_to(raw_dir.parent))
        
        # Verify structure
        assert isinstance(source_hashes, dict)
        assert isinstance(source_files, dict)
        assert len(source_hashes) == len(files)
        assert len(source_files) == len(files)
        
        # Verify all files have hashes
        for filename in files.keys():
            assert filename in source_hashes, f"Hash missing for {filename}"
            assert filename in source_files, f"File path missing for {filename}"
            assert len(source_hashes[filename]) == 64, f"Hash for {filename} should be 64 chars"
    
    def test_hash_storage_payload_format(self, mock_rip_environment):
        """Test that hash storage matches expected payload format."""
        files = mock_rip_environment["files"]
        raw_dir = mock_rip_environment["raw_dir"]
        
        # Create disc_payload structure
        source_hashes = {}
        source_files = {}
        
        for filename, filepath in files.items():
            source_hashes[filename] = calculate_file_hash(filepath)
            source_files[filename] = f"raw/{filename}"
        
        disc_payload = {
            "source_hashes": source_hashes,
            "source_files": source_files,
        }
        
        # Verify it's JSON serializable
        json_str = json.dumps(disc_payload)
        loaded = json.loads(json_str)
        
        assert loaded["source_hashes"] == source_hashes
        assert loaded["source_files"] == source_files


class TestHashVerificationAfterRip:
    """Test hash verification after rip stage completes."""
    
    def test_verify_hashes_stored_correctly(self, mock_rip_environment):
        """Test that hashes are stored correctly for verification."""
        files = mock_rip_environment["files"]
        raw_dir = mock_rip_environment["raw_dir"]
        
        # Calculate and store hashes
        source_hashes = {}
        source_files = {}
        
        for filename, filepath in files.items():
            stored_hash = calculate_file_hash(filepath)
            source_hashes[filename] = stored_hash
            source_files[filename] = f"raw/{filename}"
        
        # Verify stored hashes match actual file hashes
        for filename, filepath in files.items():
            stored_hash = source_hashes[filename]
            current_hash = calculate_file_hash(filepath)
            assert stored_hash == current_hash, f"Stored hash doesn't match for {filename}"
    
    def test_hash_verification_with_file_moves(self, mock_rip_environment, tmp_path):
        """Test that hashes can be verified after files are moved."""
        files = mock_rip_environment["files"]
        raw_dir = mock_rip_environment["raw_dir"]
        
        # Calculate hashes from source files
        source_hashes = {}
        for filename, filepath in files.items():
            source_hashes[filename] = calculate_file_hash(filepath)
        
        # Move files to new location (simulating post-process)
        dest_dir = tmp_path / "moved_files"
        dest_dir.mkdir()
        
        moved_hashes = {}
        for filename, filepath in files.items():
            dest_file = dest_dir / filename
            import shutil
            shutil.copy2(filepath, dest_file)
            moved_hashes[filename] = calculate_file_hash(dest_file)
        
        # Hashes should still match after move (copy preserves content)
        for filename in files.keys():
            assert source_hashes[filename] == moved_hashes[filename], \
                f"Hash should remain constant after move for {filename}"


class TestHashCalculationIntegration:
    """Integration tests for hash calculation in rip workflow."""
    
    def test_hash_calculation_simulates_rip_completion(self, mock_rip_environment):
        """Simulate the hash calculation that happens at end of rip."""
        files = mock_rip_environment["files"]
        raw_dir = mock_rip_environment["raw_dir"]
        
        # Simulate what happens in rip_disc after rip completes
        source_hashes = {}
        source_files = {}
        
        for filename, filepath in files.items():
            try:
                from core.utils import hash_file
                calculated_hash = hash_file(str(filepath), hash_type="sha256")
                source_hashes[filename] = calculated_hash
                source_files[filename] = str(filepath.relative_to(raw_dir.parent))
            except Exception as exc:
                pytest.fail(f"Failed to calculate hash for {filename}: {exc}")
        
        # Verify all files were hashed
        assert len(source_hashes) == len(files)
        assert len(source_files) == len(files)
        
        # Verify hashes are valid
        for filename, stored_hash in source_hashes.items():
            assert len(stored_hash) == 64, f"Invalid hash length for {filename}"
            assert all(c in '0123456789abcdef' for c in stored_hash), \
                f"Hash contains invalid characters for {filename}"

