"""#929: one machine-wide pool, at low priority, for background media work.

Prod 2026-10-19, 6 cores: the preview limiter held 6 HLS encodes while junk
detection ran 5 full-length freezedetect decodes outside it. Load hit 40 and
page loads timed out.
"""
from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from core import media_jobs


@pytest.fixture(autouse=True)
def _slots_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(media_jobs, "slot_dir", lambda: tmp_path)
    monkeypatch.setattr(media_jobs, "_POLL_SECONDS", 0.02)
    yield


def _peak_concurrency(cap: int, workers: int, hold: float = 0.15) -> int:
    active = 0
    peak = 0
    lock = threading.Lock()

    def job():
        nonlocal active, peak
        with media_jobs.media_slot(cap, wait_seconds=10):
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(hold)
            with lock:
                active -= 1

    threads = [threading.Thread(target=job) for _ in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return peak


def test_threads_never_exceed_the_limit():
    assert _peak_concurrency(cap=3, workers=10) == 3


# The holder is a fresh interpreter, not a fork: forking the pytest process
# while other tests' threads hold locks can deadlock the child (it did on CI).
_HOLDER = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from core import media_jobs
media_jobs.slot_dir = lambda: Path(sys.argv[2])
with media_jobs.media_slot(1, wait_seconds=10):
    print("ready", flush=True)
    sys.stdin.read()          # held until the test closes stdin or kills us
"""


def _start_holder(slot_dir):
    backend = Path(__file__).resolve().parents[1]
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(backend), str(slot_dir)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    ready = holder.stdout.readline().strip()
    assert ready == "ready", f"holder did not take the slot (exit {holder.poll()})"
    return holder


def test_the_limit_holds_across_processes(tmp_path):
    """Celery prefork workers are separate processes; a slot held in one
    blocks the others."""
    holder = _start_holder(tmp_path)
    try:
        with pytest.raises(TimeoutError):
            with media_jobs.media_slot(1, wait_seconds=0.3):
                pass
    finally:
        holder.stdin.close()
        holder.wait(10)
    with media_jobs.media_slot(1, wait_seconds=2):   # freed once the holder exits
        pass


def test_a_dead_holder_releases_its_slot(tmp_path):
    """The kernel drops an flock when its process dies: no stale slots."""
    holder = _start_holder(tmp_path)
    holder.kill()
    holder.wait(10)
    with media_jobs.media_slot(1, wait_seconds=2):
        pass


def test_low_priority_wraps_with_ionice_and_nice(monkeypatch):
    monkeypatch.setattr(media_jobs.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert media_jobs.low_priority(["ffmpeg", "-i", "x"]) == [
        "/usr/bin/ionice", "-c3", "-t", "/usr/bin/nice", "-n", "19", "ffmpeg", "-i", "x",
    ]


def test_low_priority_runs_the_command_where_the_tools_are_missing(monkeypatch):
    monkeypatch.setattr(media_jobs.shutil, "which", lambda name: None)
    assert media_jobs.low_priority(["ffmpeg"]) == ["ffmpeg"]


def test_run_media_holds_a_slot_and_lowers_priority(monkeypatch):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        with pytest.raises(TimeoutError):          # the slot is held during the run
            with media_jobs.media_slot(1, wait_seconds=0.1):
                pass
        return "done"

    monkeypatch.setattr(media_jobs.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(media_jobs.subprocess, "run", fake_run)
    assert media_jobs.run_media(["ffmpeg"], max_parallel=1) == "done"
    assert seen["cmd"][-1] == "ffmpeg" and "/usr/bin/nice" in seen["cmd"]


def test_junk_detection_goes_through_the_pool(monkeypatch, tmp_path):
    """freezedetect decodes a whole title; it ran outside any limit."""
    from core import ffmpeg_detection

    calls = []
    monkeypatch.setattr(ffmpeg_detection, "run_media",
                        lambda cmd, **kw: calls.append(cmd) or type("R", (), {"stdout": "", "stderr": ""})())
    ffmpeg_detection._detect_freeze(tmp_path / "t.mkv", 600.0)
    assert calls and "freezedetect=n=-60dB:d=2" in calls[0]


def test_the_preview_limiter_is_the_shared_pool(monkeypatch):
    from workers import tasks

    used = []

    class Slot:
        def __init__(self, n):
            used.append(n)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(media_jobs, "media_slot", Slot)
    with tasks.ffmpeg_semaphore(4):
        pass
    assert used == [4]


def test_default_limit_leaves_a_core_free(tmp_path, monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "_SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(settings.os, "cpu_count", lambda: 6)
    assert settings._default_settings()["preview_max_parallel"] == 5
