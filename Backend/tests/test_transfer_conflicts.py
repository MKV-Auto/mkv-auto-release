"""
Tests for transfer conflict resolution.
"""
import pytest
from pathlib import Path
from core.transfer.utils.conflicts import (
    check_conflict,
    resolve_conflict,
    generate_unique_name,
)


def test_check_conflict_file_exists(tmp_path):
    """Test conflict detection for existing file."""
    file_path = tmp_path / "test.mkv"
    file_path.write_text("test data")
    
    assert check_conflict(file_path) is True


def test_check_conflict_file_not_exists(tmp_path):
    """Test conflict detection for non-existent file."""
    file_path = tmp_path / "test.mkv"
    
    assert check_conflict(file_path) is False


def test_resolve_conflict_overwrite(tmp_path):
    """Test overwrite conflict resolution."""
    file_path = tmp_path / "test.mkv"
    file_path.write_text("existing")
    
    resolved, should_proceed = resolve_conflict(file_path, "overwrite")
    assert resolved == file_path
    assert should_proceed is True


def test_resolve_conflict_skip(tmp_path):
    """Test skip conflict resolution."""
    file_path = tmp_path / "test.mkv"
    file_path.write_text("existing")
    
    resolved, should_proceed = resolve_conflict(file_path, "skip")
    assert resolved == file_path
    assert should_proceed is False


def test_resolve_conflict_rename(tmp_path):
    """Test rename conflict resolution."""
    file_path = tmp_path / "test.mkv"
    file_path.write_text("existing")
    
    resolved, should_proceed = resolve_conflict(file_path, "rename")
    assert resolved != file_path
    assert resolved.name == "test (1).mkv"
    assert should_proceed is True


def test_resolve_conflict_fail(tmp_path):
    """Test fail conflict resolution."""
    file_path = tmp_path / "test.mkv"
    file_path.write_text("existing")
    
    with pytest.raises(FileExistsError):
        resolve_conflict(file_path, "fail")


def test_resolve_conflict_no_conflict(tmp_path):
    """Test resolution when no conflict exists."""
    file_path = tmp_path / "test.mkv"
    
    resolved, should_proceed = resolve_conflict(file_path, "overwrite")
    assert resolved == file_path
    assert should_proceed is True


def test_generate_unique_name(tmp_path):
    """Test unique name generation."""
    base_path = tmp_path / "test.mkv"
    base_path.write_text("existing")
    
    unique = generate_unique_name(base_path)
    assert unique.name == "test (1).mkv"
    assert not unique.exists()


def test_generate_unique_name_multiple(tmp_path):
    """Test unique name generation with multiple existing files."""
    base_path = tmp_path / "test.mkv"
    base_path.write_text("existing")
    
    (tmp_path / "test (1).mkv").write_text("existing")
    
    unique = generate_unique_name(base_path)
    assert unique.name == "test (2).mkv"
    assert not unique.exists()











