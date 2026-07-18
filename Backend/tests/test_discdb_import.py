"""Unit tests for core.discdb_import: _normalize_mount, _collect_files, hash_log_file."""
import pytest
from pathlib import Path

from core.discdb_import import (
    _normalize_mount,
    _collect_files,
    hash_log_file,
    DiscHashInfo,
)


# --- _normalize_mount (pure) ---


def test_normalize_mount_single_letter():
    assert _normalize_mount("C") == Path("C:\\")


def test_normalize_mount_drive_with_colon():
    assert _normalize_mount("D:") == Path("D:\\")


def test_normalize_mount_unix_path():
    assert _normalize_mount("/mnt/disc") == Path("/mnt/disc")


# --- _collect_files (tmp_path) ---


def test_collect_files_with_pattern_sorted_by_name(tmp_path):
    (tmp_path / "b.m2ts").write_text("")
    (tmp_path / "a.m2ts").write_text("")
    (tmp_path / "c.m2ts").write_text("")
    got = _collect_files(tmp_path, "*.m2ts")
    assert [p.name for p in got] == ["a.m2ts", "b.m2ts", "c.m2ts"]


def test_collect_files_nonexistent_base_returns_empty():
    got = _collect_files(Path("/nonexistent/path/xyz"), "*.m2ts")
    assert got == []


# --- hash_log_file (patch _calculate_hash) ---


def test_hash_log_file_parses_hsh_lines_and_uses_calculated_hash(tmp_path, monkeypatch):
    monkeypatch.setattr("core.discdb_import._calculate_hash", lambda files: "abc123", raising=False)
    log = tmp_path / "makemkv.log"
    log.write_text(
        "HSH:0,file.m2ts,2020-01-01T00:00:00,1000\n"
        "HSH:1,other.m2ts,2020-01-02T12:00:00,2000\n",
        encoding="utf-8",
    )
    info = hash_log_file(log)
    assert info is not None
    assert isinstance(info, DiscHashInfo)
    assert info.hash == "abc123"
    assert len(info.files) == 2
    assert info.files[0].index == 0
    assert info.files[0].name == "file.m2ts"
    assert info.files[0].size == 1000
    assert info.files[1].index == 1
    assert info.files[1].name == "other.m2ts"
    assert info.files[1].size == 2000


def test_hash_log_file_nonexistent_returns_none():
    assert hash_log_file(Path("/nonexistent/makemkv.log")) is None


def test_hash_log_file_no_hsh_lines_returns_none(tmp_path):
    log = tmp_path / "makemkv.log"
    log.write_text("DRIVE: 0\nMSG: Some message\n", encoding="utf-8")
    assert hash_log_file(log) is None
