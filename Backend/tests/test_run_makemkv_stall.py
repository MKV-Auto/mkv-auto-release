"""Tests for makemkv stdout stall watchdog (RIP_OUTPUT_STALL_SECONDS)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import core.utils as u


def test_get_rip_output_stall_seconds(monkeypatch):
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "42")
    assert u.get_rip_output_stall_seconds() == 42
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "0")
    assert u.get_rip_output_stall_seconds() == 0
    monkeypatch.delenv("RIP_OUTPUT_STALL_SECONDS", raising=False)
    assert u.get_rip_output_stall_seconds() == 300
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "not-a-number")
    assert u.get_rip_output_stall_seconds() == 300


def test_run_makemkv_stall_raises_make_mkv_stall_error(tmp_path, monkeypatch):
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "1")
    stopped = False

    def kill_side_effect():
        nonlocal stopped
        stopped = True

    mock_p = MagicMock()
    mock_p.pid = 424242
    mock_p.kill = MagicMock(side_effect=kill_side_effect)
    mock_p.wait = MagicMock(return_value=-9)

    class StuckStdout:
        def __init__(self):
            self.sent = False

        def __iter__(self):
            return self

        def __next__(self):
            import time as time_mod

            if not self.sent:
                self.sent = True
                return "line1\n"
            while not stopped:
                time_mod.sleep(0.02)
            raise StopIteration

    mock_p.stdout = StuckStdout()

    log_path = tmp_path / "makemkv_progress.log"
    with patch.object(u, "MAKEMKVCON_PATH", "/fake/makemkvcon"):
        with patch.object(u.subprocess, "Popen", return_value=mock_p):
            with pytest.raises(u.MakeMKVStallError, match="No output from MakeMKV"):
                u.run_makemkv("mkv disc:0 all /tmp/out", log_path=log_path)

    mock_p.kill.assert_called()


def test_run_makemkv_stall_disabled_zero(monkeypatch, tmp_path):
    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "0")
    mock_p = MagicMock()
    mock_p.pid = 1
    mock_p.wait = MagicMock(return_value=0)
    mock_p.stdout = iter(["done\n"])

    log_path = tmp_path / "p.log"
    with patch.object(u, "MAKEMKVCON_PATH", "/fake/makemkvcon"):
        with patch.object(u.subprocess, "Popen", return_value=mock_p):
            out, pid = u.run_makemkv("mkv disc:0 all /tmp/out", log_path=log_path)
    assert "done" in out
    assert pid == 1
    mock_p.kill.assert_not_called()
