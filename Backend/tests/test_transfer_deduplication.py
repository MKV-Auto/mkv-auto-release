"""
Tests for transfer deduplication system.
"""
import pytest
from pathlib import Path
from core.transfer.utils.deduplication import (
    check_file_exists,
    should_skip_transfer,
    get_destination_file_hash,
)


def test_check_file_exists_match(tmp_path):
    """Test file existence check with matching hash."""
    dest_file = tmp_path / "dest.mkv"
    dest_file.write_bytes(b"test data")
    
    from core.transfer.validation import calculate_file_hash
    expected_hash = calculate_file_hash(dest_file)
    
    class MockConfig:
        pass
    
    exists, actual_hash = check_file_exists(dest_file, expected_hash, MockConfig())
    assert exists is True
    assert actual_hash == expected_hash


def test_check_file_exists_mismatch(tmp_path):
    """Test file existence check with non-matching hash."""
    dest_file = tmp_path / "dest.mkv"
    dest_file.write_bytes(b"test data")
    
    wrong_hash = "a" * 64
    
    class MockConfig:
        pass
    
    exists, actual_hash = check_file_exists(dest_file, wrong_hash, MockConfig())
    assert exists is False
    assert actual_hash != wrong_hash


def test_check_file_exists_not_exists(tmp_path):
    """Test file existence check when file doesn't exist."""
    dest_file = tmp_path / "nonexistent.mkv"
    
    class MockConfig:
        pass
    
    exists, actual_hash = check_file_exists(dest_file, "a" * 64, MockConfig())
    assert exists is False
    assert actual_hash is None


def test_should_skip_transfer_skip_strategy_match(tmp_path):
    """conflict_resolution=='skip' + hash match at destination → skip transfer."""
    class MockConfig:
        conflict_resolution = "skip"
    
    source_file = tmp_path / "source.mkv"
    dest_file = tmp_path / "dest.mkv"
    source_file.write_bytes(b"test data")
    dest_file.write_bytes(b"test data")
    
    from core.transfer.validation import calculate_file_hash
    source_hash = calculate_file_hash(source_file)
    
    should_skip, existing_hash = should_skip_transfer("job1", source_file, dest_file, source_hash, MockConfig())
    assert should_skip is True
    assert existing_hash == source_hash


def test_should_skip_transfer_skip_strategy_mismatch(tmp_path):
    """conflict_resolution=='skip' but destination hash differs → do not skip."""
    class MockConfig:
        conflict_resolution = "skip"
    
    source_file = tmp_path / "source.mkv"
    dest_file = tmp_path / "dest.mkv"
    source_file.write_bytes(b"test data")
    dest_file.write_bytes(b"different data")
    
    from core.transfer.validation import calculate_file_hash
    source_hash = calculate_file_hash(source_file)
    
    should_skip, existing_hash = should_skip_transfer("job1", source_file, dest_file, source_hash, MockConfig())
    assert should_skip is False


@pytest.mark.parametrize("strategy", ["overwrite", "fail", "rename", None])
def test_should_skip_transfer_non_skip_strategy_never_skips(tmp_path, strategy):
    """
    Only conflict_resolution=='skip' triggers the hash pre-flight. All other
    strategies (overwrite / fail / rename) must never short-circuit on hash
    match — path collision drives their semantics.
    """
    class MockConfig:
        conflict_resolution = strategy

    source_file = tmp_path / "source.mkv"
    dest_file = tmp_path / "dest.mkv"
    source_file.write_bytes(b"test data")
    dest_file.write_bytes(b"test data")

    from core.transfer.validation import calculate_file_hash
    source_hash = calculate_file_hash(source_file)

    should_skip, existing_hash = should_skip_transfer("job1", source_file, dest_file, source_hash, MockConfig())
    assert should_skip is False


def test_get_destination_file_hash(tmp_path):
    """Test getting hash of destination file."""
    dest_file = tmp_path / "dest.mkv"
    dest_file.write_bytes(b"test data")
    
    class MockConfig:
        pass
    
    hash_value = get_destination_file_hash(dest_file, MockConfig())
    assert hash_value is not None
    assert len(hash_value) == 64


def test_get_destination_file_hash_not_exists(tmp_path):
    """Test getting hash when file doesn't exist."""
    dest_file = tmp_path / "nonexistent.mkv"
    
    class MockConfig:
        pass
    
    hash_value = get_destination_file_hash(dest_file, MockConfig())
    assert hash_value is None











