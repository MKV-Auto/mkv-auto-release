"""
Tests for transfer cleanup system.

The historical ``cleanup_source`` per-config toggle was dropped; post-transfer
source cleanup is now unconditional. ``should_cleanup`` always returns True.
The ``cleanup_source_safe`` helper still exercises the actual file-removal
mechanics — those are what the reconciler and the sync completion path both
call.
"""
import pytest
from pathlib import Path
from core.transfer.monitoring import (
    should_cleanup,
    cleanup_source_safe,
)


def test_should_cleanup_is_always_true():
    """Cleanup is unconditional after the toggle drop."""
    class MockConfig:
        pass

    assert should_cleanup("job1", MockConfig()) is True


def test_cleanup_source_safe_removes_file(tmp_path):
    class MockConfig:
        pass

    source_file = tmp_path / "source.mkv"
    source_file.write_text("test data")

    success, error = cleanup_source_safe("job1", MockConfig(), [source_file])

    assert success is True
    assert not source_file.exists()


def test_cleanup_source_safe_multiple_files(tmp_path):
    class MockConfig:
        pass

    file1 = tmp_path / "file1.mkv"
    file2 = tmp_path / "file2.mkv"
    file1.write_text("data1")
    file2.write_text("data2")

    success, error = cleanup_source_safe("job1", MockConfig(), [file1, file2])

    assert success is True
    assert not file1.exists()
    assert not file2.exists()


def test_cleanup_source_safe_nonexistent_file(tmp_path):
    class MockConfig:
        pass

    nonexistent = tmp_path / "nonexistent.mkv"

    success, error = cleanup_source_safe("job1", MockConfig(), [nonexistent])

    assert success is True  # idempotent — no-op when target already gone


def test_cleanup_source_safe_empty_list(tmp_path):
    class MockConfig:
        pass

    success, error = cleanup_source_safe("job1", MockConfig(), [])

    assert success is True
