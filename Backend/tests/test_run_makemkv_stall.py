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


# --- #909: a drive hung in the kernel ------------------------------------

class _NeverEndingStdout:
    """stdout of a makemkvcon whose stuck thread keeps the pipe open: one
    line, then nothing, not even after SIGKILL."""

    def __init__(self, release):
        self.sent = False
        self.release = release

    def __iter__(self):
        return self

    def __next__(self):
        if not self.sent:
            self.sent = True
            return "line1\n"
        self.release.wait(30)
        raise StopIteration


def _stuck_proc(*, exits_after_kill: bool, release, reaped):
    import subprocess as sp

    p = MagicMock()
    p.pid = 515151

    def wait(timeout=None):
        if exits_after_kill:
            return -9
        if timeout is not None:
            raise sp.TimeoutExpired("makemkvcon", timeout)
        reaped.set()               # only the reaper waits without a timeout
        release.wait(30)
        return -9

    p.wait = MagicMock(side_effect=wait)
    p.stdout = _NeverEndingStdout(release)
    return p


def test_unkillable_makemkvcon_fails_the_rip_instead_of_hanging_it(tmp_path, monkeypatch):
    """Prod 2026-10-18: the watchdog killed makemkvcon at 00:25, a thread stayed
    in submit_bio_wait, and the rip task never returned (job 'ripping' for 8.5 h)."""
    import threading
    import time

    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "1")
    monkeypatch.setattr(u, "STALL_KILL_GRACE_SECONDS", 0.2)
    release, reaped = threading.Event(), threading.Event()
    proc = _stuck_proc(exits_after_kill=False, release=release, reaped=reaped)
    try:
        t0 = time.monotonic()
        with patch.object(u, "MAKEMKVCON_PATH", "/fake/makemkvcon"):
            with patch.object(u.subprocess, "Popen", return_value=proc):
                with pytest.raises(u.MakeMKVDriveHungError) as exc:
                    u.run_makemkv("mkv disc:0 all /tmp/out", log_path=tmp_path / "p.log")
        assert time.monotonic() - t0 < 10
        msg = str(exc.value)
        assert msg.startswith("No output from MakeMKV for 1s")   # still counts as a stall
        assert u.DRIVE_HUNG_MARKER in msg
        assert isinstance(exc.value, u.MakeMKVStallError)
        proc.kill.assert_called()
        assert reaped.wait(2), "the stuck process must be handed to a reaper"
    finally:
        release.set()


def test_killed_makemkvcon_whose_pipe_stays_open_still_ends_the_rip(tmp_path, monkeypatch):
    """The process exits on SIGKILL but the reader never sees EOF: the read
    loop used to wait on its queue forever."""
    import threading
    import time

    monkeypatch.setenv("RIP_OUTPUT_STALL_SECONDS", "1")
    release, reaped = threading.Event(), threading.Event()
    proc = _stuck_proc(exits_after_kill=True, release=release, reaped=reaped)
    try:
        t0 = time.monotonic()
        with patch.object(u, "MAKEMKVCON_PATH", "/fake/makemkvcon"):
            with patch.object(u.subprocess, "Popen", return_value=proc):
                with pytest.raises(u.MakeMKVStallError) as exc:
                    u.run_makemkv("mkv disc:0 all /tmp/out", log_path=tmp_path / "p.log")
        assert time.monotonic() - t0 < 15
        assert not isinstance(exc.value, u.MakeMKVDriveHungError)
    finally:
        release.set()


def test_hung_drive_verdict_says_to_replug():
    from types import SimpleNamespace

    from core.rip_failure_analysis import DRIVE_HUNG_REMEDY, analyze_rip_failure

    job = SimpleNamespace(
        id="j", disc_id=None, drive_by_id_serial=None,
        error_reason=(
            "No output from MakeMKV for 300s, and it could not be stopped: a read from "
            "the drive never returned. The drive or its USB connection has hung."
        ),
    )
    verdict = analyze_rip_failure(job, db=None, log_text="")
    assert verdict.classification == "drive_fault"
    assert verdict.remedy == DRIVE_HUNG_REMEDY
    assert "Unplug" in verdict.augment(job.error_reason)
