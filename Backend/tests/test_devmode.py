"""Unit tests for core.devmode: _format_list, _format_mismatches, build_validation_report, _gather_files, compare_directories."""
import pytest
from pathlib import Path

from core.devmode import (
    _format_list,
    _format_mismatches,
    build_validation_report,
    _gather_files,
    compare_directories,
)


# --- _format_list (pure) ---


def test_format_list_empty():
    assert _format_list([]) == "<p>None</p>"


def test_format_list_items():
    got = _format_list(["a", "b"])
    assert got == "<ul><li><code>a</code></li><li><code>b</code></li></ul>"


# --- _format_mismatches (pure) ---


def test_format_mismatches_empty():
    assert _format_mismatches([]) == "<p>None</p>"


def test_format_mismatches_items():
    got = _format_mismatches([{"path": "x", "reason": "content differs"}])
    assert "<code>x</code>" in got
    assert "content differs" in got


# --- build_validation_report (pure) ---


def test_build_validation_report_includes_disc_hash_status_and_sections():
    diff = {"status": "mismatch", "missing_files": ["a"], "extra_files": [], "mismatched_files": []}
    html = build_validation_report("HASH123", Path("/e"), Path("/a"), diff)
    assert "HASH123" in html
    assert "mismatch" in html
    assert "Missing" in html
    assert "Extra" in html
    assert "Mismatched" in html
    assert "<code>a</code>" in html


# --- _gather_files (tmp_path) ---


def test_gather_files_collects_all_when_exclude_empty(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.txt").write_text("x")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.json").write_text("[]")
    got = _gather_files(tmp_path, [])
    assert "a.json" in got
    assert "b.txt" in got
    assert "sub/c.json" in got


def test_gather_files_excludes_by_pattern(tmp_path):
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.txt").write_text("x")
    got = _gather_files(tmp_path, ["*.txt"])
    assert "a.json" in got
    assert "b.txt" not in got


# --- compare_directories (tmp_path, _file_hash via real files) ---


def test_compare_directories_missing_extra_mismatched(tmp_path):
    expected = tmp_path / "expected"
    actual = tmp_path / "actual"
    expected.mkdir()
    actual.mkdir()

    (expected / "only_expected").write_text("e")
    (actual / "only_actual").write_text("a")
    (expected / "same").write_text("same")
    (actual / "same").write_text("same")
    (expected / "diff").write_text("v1")
    (actual / "diff").write_text("v2")

    got = compare_directories(expected, actual, exclude=[])
    assert got["status"] == "mismatch"
    assert got["missing_files"] == ["only_expected"]
    assert got["extra_files"] == ["only_actual"]
    assert len(got["mismatched_files"]) == 1
    assert got["mismatched_files"][0]["path"] == "diff"
    assert "differs" in got["mismatched_files"][0]["reason"]


def test_compare_directories_matched(tmp_path):
    d = tmp_path / "both"
    d.mkdir()
    (d / "f").write_text("x")
    got = compare_directories(d, d, exclude=[])
    assert got["status"] == "matched"
    assert got["missing_files"] == []
    assert got["extra_files"] == []
    assert got["mismatched_files"] == []
