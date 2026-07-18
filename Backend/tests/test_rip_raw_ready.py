"""Unit tests for rip_raw_ready (quiescence, rglob sizes, ffprobe gate)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from workers.rip_raw_ready import mkv_sizes_by_relpath, probe_raw_mkv_ready, wait_ripped_mkvs_quiescent


def test_mkv_sizes_by_relpath_includes_nested(tmp_path: Path) -> None:
    (tmp_path / "a.mkv").write_bytes(b"x")
    nested = tmp_path / "sub" / "deep"
    nested.mkdir(parents=True)
    (nested / "b.mkv").write_bytes(b"yy")
    sizes = mkv_sizes_by_relpath(tmp_path)
    assert sizes["a.mkv"] == 1
    assert sizes["sub/deep/b.mkv"] == 2


def test_wait_ripped_mkvs_quiescent_stable_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MKVAUTO_RIP_SHORT_INTERVAL_SECONDS", "1")
    monkeypatch.setenv("MKVAUTO_RIP_QUIESCENCE_STABLE_SECONDS", "1")
    (tmp_path / "t.mkv").write_bytes(b"stable-bytes")
    wait_ripped_mkvs_quiescent(tmp_path, ["t.mkv"])


@pytest.mark.skipif(not shutil.which("ffprobe"), reason="ffprobe not installed")
def test_probe_raw_mkv_ready_rejects_garbage(tmp_path: Path) -> None:
    p = tmp_path / "not-really.mkv"
    p.write_bytes(b"not a matroska container" * 50)
    ok, err = probe_raw_mkv_ready(p)
    assert ok is False
    assert err


def test_probe_raw_mkv_ready_skip_ffprobe_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When integration tests set SKIP globally, probe_ripped_mkvs_ready short-circuits."""
    from workers import rip_raw_ready

    monkeypatch.setenv("MKVAUTO_RIP_VERIFY_SKIP_FFPROBE", "1")
    ok, err = rip_raw_ready.probe_ripped_mkvs_ready(tmp_path, {"tid": "x.mkv"})
    assert ok is True
    assert err == ""
