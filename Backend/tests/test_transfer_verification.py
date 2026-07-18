"""
Tests for transfer verification system.
"""
import pytest
from pathlib import Path
from core.transfer.validation import (
    calculate_file_hash,
    verify_transferred_file,
)


def test_calculate_file_hash(tmp_path):
    """Test hash calculation."""
    test_file = tmp_path / "test.bin"
    test_data = b"test data for hashing"
    test_file.write_bytes(test_data)
    
    hash_value = calculate_file_hash(test_file)
    assert hash_value
    assert len(hash_value) == 64  # SHA256 hex digest length


def test_calculate_file_hash_consistent(tmp_path):
    """Test that hash calculation is consistent."""
    test_file = tmp_path / "test.bin"
    test_data = b"test data"
    test_file.write_bytes(test_data)
    
    hash1 = calculate_file_hash(test_file)
    hash2 = calculate_file_hash(test_file)
    assert hash1 == hash2


def test_verify_transferred_file_match(tmp_path):
    """Test verification when hashes match."""
    test_file = tmp_path / "test.bin"
    test_data = b"test data"
    test_file.write_bytes(test_data)
    
    expected_hash = calculate_file_hash(test_file)
    verified, error = verify_transferred_file(test_file, expected_hash)
    
    assert verified is True
    assert error is None


def test_verify_transferred_file_mismatch(tmp_path):
    """Test verification when hashes don't match."""
    test_file = tmp_path / "test.bin"
    test_file.write_bytes(b"test data")
    
    wrong_hash = "a" * 64  # Wrong hash
    verified, error = verify_transferred_file(test_file, wrong_hash)
    
    assert verified is False
    assert error
    assert "mismatch" in error.lower()


def test_verify_transferred_file_not_exists(tmp_path):
    """Test verification when file doesn't exist."""
    test_file = tmp_path / "nonexistent.bin"
    
    verified, error = verify_transferred_file(test_file, "a" * 64)
    assert verified is False
    assert "not exist" in error.lower()











