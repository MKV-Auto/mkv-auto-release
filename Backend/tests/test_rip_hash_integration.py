"""
Tests for hash calculation integration in rip_disc.
Tests that hashes are calculated and stored correctly at end of rip.
"""
import pytest
import uuid
from pathlib import Path
from unittest.mock import Mock, MagicMock, patch
from sqlalchemy.orm import Session

from core.transfer.validation import calculate_file_hash


@pytest.fixture
def mock_rip_workdir(tmp_path):
    """Create a mock rip workdir with files."""
    workdir = tmp_path / "rip_workdir"
    workdir.mkdir()
    
    # Create sample MKV files
    files = {}
    title_ids = []
    for i in range(3):
        filename = f"title_{i+1:03d}.mkv"
        filepath = workdir / filename
        content = b"mkv content " * (100 + i * 50)
        filepath.write_bytes(content)
        files[filename] = filepath
        title_ids.append(str(uuid.uuid4()))
    
    return workdir, files, title_ids


class TestRipHashCalculationIntegration:
    """Test hash calculation integration in rip_disc."""
    
    def test_hash_calculation_stores_in_disc_payload(self, mock_rip_workdir):
        """Test that hash calculation stores results in disc_payload."""
        workdir, files, title_ids = mock_rip_workdir
        
        # Simulate the hash calculation logic from rip_disc
        # ripped_files now uses title_id keys
        ripped_files = {}
        source_hashes = {}
        source_files = {}
        
        for i, (filename, filepath) in enumerate(files.items()):
            title_id = title_ids[i]
            try:
                from core.utils import hash_file
                calculated_hash = hash_file(str(filepath), hash_type="sha256")
                ripped_files[title_id] = str(filepath.relative_to(workdir))
                source_hashes[title_id] = calculated_hash  # Hashes also use title_id keys
                source_files[title_id] = str(filepath.relative_to(workdir))
            except Exception as exc:
                pytest.fail(f"Failed to calculate hash for {filename}: {exc}")
        
        # Verify structure matches expected format
        disc_payload = {
            "source_hashes": source_hashes,
            "source_files": source_files,
            "ripped_files": ripped_files,
        }
        
        assert "source_hashes" in disc_payload
        assert "source_files" in disc_payload
        assert "ripped_files" in disc_payload
        assert len(disc_payload["source_hashes"]) == len(files)
        assert len(disc_payload["source_files"]) == len(files)
        assert len(disc_payload["ripped_files"]) == len(files)
        
        # Verify hashes are correct (using title_id keys)
        for i, (filename, filepath) in enumerate(files.items()):
            title_id = title_ids[i]
            expected_hash = calculate_file_hash(filepath)
            assert disc_payload["source_hashes"][title_id] == expected_hash
            assert disc_payload["source_files"][title_id] == filename  # Relative path
            # Verify keys are title_ids
            assert len(title_id) == 36 and '-' in title_id, f"Expected UUID format, got {title_id}"
    
    def test_hash_calculation_handles_missing_files(self, tmp_path):
        """Test that hash calculation handles missing files gracefully."""
        import uuid
        workdir = tmp_path / "rip_workdir"
        workdir.mkdir()
        
        # Create one existing file
        existing_file = workdir / "title_001.mkv"
        existing_file.write_bytes(b"content")
        title1_id = str(uuid.uuid4())
        
        # Build ripped_files with title_id keys (one exists, one doesn't)
        missing_title_id = str(uuid.uuid4())
        ripped_files = {
            title1_id: "title_001.mkv",
            missing_title_id: "missing_file.mkv",  # File doesn't exist
        }
        
        source_hashes = {}
        source_files = {}
        errors = []
        
        for title_id, rel_path in ripped_files.items():
            source_path = workdir / rel_path
            if source_path.exists():
                try:
                    from core.utils import hash_file
                    calculated_hash = hash_file(str(source_path), hash_type="sha256")
                    source_hashes[title_id] = calculated_hash
                    source_files[title_id] = rel_path
                except Exception as exc:
                    errors.append(f"Failed to calculate hash for {title_id}: {exc}")
            else:
                errors.append(f"File not found: {rel_path} (title_id: {title_id})")
        
        # Verify that missing files are handled
        assert missing_title_id not in source_hashes
        assert title1_id in source_hashes
        assert len(errors) > 0
        assert any("missing_file.mkv" in err for err in errors)
    
    def test_hash_calculation_with_subdirectories(self, tmp_path):
        """Test hash calculation when files are in subdirectories."""
        workdir = tmp_path / "rip_workdir"
        subdir = workdir / "subdir"
        subdir.mkdir(parents=True)
        
        file1 = subdir / "title_001.mkv"
        file1.write_bytes(b"content 1" * 100)
        
        file2 = subdir / "title_002.mkv"
        file2.write_bytes(b"content 2" * 100)
        
        title1_id = str(uuid.uuid4())
        title2_id = str(uuid.uuid4())
        # ripped_files now uses title_id keys
        ripped_files = {
            title1_id: "subdir/title_001.mkv",
            title2_id: "subdir/title_002.mkv",
        }
        
        source_hashes = {}
        source_files = {}
        
        for title_id, rel_path in ripped_files.items():
            source_path = workdir / rel_path
            if source_path.exists():
                from core.utils import hash_file
                calculated_hash = hash_file(str(source_path), hash_type="sha256")
                source_hashes[title_id] = calculated_hash
                source_files[title_id] = rel_path
        
        # Verify hashes are correct (using title_id keys)
        assert len(source_hashes) == 2
        assert source_hashes[title1_id] == calculate_file_hash(file1)
        assert source_hashes[title2_id] == calculate_file_hash(file2)
        assert source_files[title1_id] == "subdir/title_001.mkv"
        assert source_files[title2_id] == "subdir/title_002.mkv"

