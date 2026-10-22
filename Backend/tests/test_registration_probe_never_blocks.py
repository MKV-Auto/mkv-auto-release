"""#908: the MakeMKV registration probe must not touch the drive on page loads.

Every registered ``makemkvcon`` run opens every optical drive at startup. The
registration probe ran from the health endpoints the frontend calls on page
load, cached for 5 minutes: it spun up idle discs, sent commands to drives
mid-rip, and with the drive hung (prod, 2026-10-18) each request left a stuck
process behind and leaked the API thread that waited on it.
"""
from __future__ import annotations

import subprocess
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from core import makemkv_updater as mu

EXPIRED = 'MSG:5055,0,0,"Evaluation period has expired, shareware functionality unavailable.","..."\n'
CLEAN = 'MSG:1005,0,1,"MakeMKV v1.18.4 linux(x64-release) started","%1 started","x"\n'


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(mu, "get_makemkvcon_path", lambda: "/fake/makemkvcon")
    monkeypatch.setattr(mu, "_reg_status_cache",
                        {"expired": None, "message": None, "key": None, "ts": None, "failed_ts": None})
    mu._reg_refresh_inflight.clear()
    yield
    mu._reg_refresh_inflight.clear()


@pytest.fixture
def probes(monkeypatch):
    """Record every probe run; return queued outputs (or raise them)."""
    calls: list[list[str]] = []
    outputs: list = []

    def fake(cmd, *, timeout, env=None, **kw):
        calls.append(cmd)
        out = outputs.pop(0) if outputs else CLEAN
        if isinstance(out, Exception):
            raise out
        return SimpleNamespace(stdout=out, stderr="", returncode=0)

    monkeypatch.setattr(mu, "_run_bounded", fake)
    monkeypatch.setattr(mu, "_drives_busy", lambda: None)
    return SimpleNamespace(calls=calls, outputs=outputs)


def _seed(expired: bool, age: float, message: str = "cached") -> None:
    mu._reg_status_cache.update(expired=expired, message=message, key=None, ts=time.monotonic() - age)


def test_a_fresh_result_is_served_without_a_probe(probes):
    _seed(True, age=60)
    assert mu.get_registration_status() == (True, "cached", None)
    assert probes.calls == []


def test_the_cache_lives_for_hours_not_minutes(probes):
    _seed(False, age=30 * 60)                      # 30 min: was a re-probe under the 5-min TTL
    mu.get_registration_status()
    assert probes.calls == []
    assert mu._REG_STATUS_CACHE_TTL >= 3600


def test_a_stale_result_is_served_at_once_and_refreshed_in_the_background(probes, monkeypatch):
    started = []
    monkeypatch.setattr(mu, "_refresh_registration_in_background", lambda: started.append(1))
    _seed(False, age=mu._REG_STATUS_CACHE_TTL + 1)
    assert mu.get_registration_status() == (False, "cached", None)
    assert probes.calls == [] and started == [1]   # the caller never waits on makemkvcon


def test_background_refresh_is_single_flight(probes, monkeypatch):
    gate = threading.Event()

    def slow(cmd, *, timeout, env=None, **kw):
        probes.calls.append(cmd)
        gate.wait(5)
        return SimpleNamespace(stdout=CLEAN, stderr="", returncode=0)

    monkeypatch.setattr(mu, "_run_bounded", slow)
    _seed(False, age=mu._REG_STATUS_CACHE_TTL + 1)
    for _ in range(5):
        mu.get_registration_status()
    gate.set()
    for _ in range(50):
        if not mu._reg_refresh_inflight.is_set():
            break
        time.sleep(0.02)
    assert len(probes.calls) == 1


def test_with_no_result_yet_the_first_call_probes(probes):
    probes.outputs.append(EXPIRED)
    expired, msg, _ = mu.get_registration_status()
    assert expired is True and "expired" in msg
    assert len(probes.calls) == 1


@pytest.mark.parametrize("cached", [True, False])
def test_no_probe_while_a_drive_is_busy(probes, monkeypatch, cached):
    monkeypatch.setattr(mu, "_drives_busy", lambda: "makemkvcon is running")
    if cached:
        _seed(True, age=mu._REG_STATUS_CACHE_TTL + 1)
    result = mu.get_registration_status(force_refresh=True)
    assert probes.calls == []
    assert result == ((True, "cached", None) if cached else (False, None, None))


def test_a_timeout_backs_off_instead_of_retrying_per_request(probes):
    probes.outputs.append(subprocess.TimeoutExpired("makemkvcon", 30))
    assert mu.get_registration_status() == (False, None, None)
    for _ in range(3):
        mu.get_registration_status()
        mu.get_registration_status(force_refresh=True)
    assert len(probes.calls) == 1                  # one stuck process, not one per page load


def test_the_backoff_ends(probes):
    probes.outputs.append(subprocess.TimeoutExpired("makemkvcon", 30))
    mu.get_registration_status()
    mu._reg_status_cache["failed_ts"] = time.monotonic() - mu._REG_PROBE_FAILURE_BACKOFF - 1
    mu.get_registration_status()
    assert len(probes.calls) == 2


def test_health_endpoint_never_waits_on_makemkv_with_a_cached_result(probes, test_db):
    from api.main import app

    _seed(False, age=mu._REG_STATUS_CACHE_TTL + 1)
    started = []
    orig = mu._refresh_registration_in_background
    mu._refresh_registration_in_background = lambda: started.append(1)
    try:
        r = TestClient(app).get("/system/makemkv/health")
    finally:
        mu._refresh_registration_in_background = orig
    assert r.status_code == 200, r.text
    assert probes.calls == []


def test_key_check_is_refused_while_a_drive_is_busy(monkeypatch):
    monkeypatch.setattr(mu, "_drives_busy", lambda: "/dev/sr0 is busy (rip)")
    ran = []
    monkeypatch.setattr(mu, "_probe_key_in_sandbox", lambda *a, **k: ran.append(1))
    with pytest.raises(mu.MakeMKVUpdateError, match="being read"):
        mu.set_registration_key("T-" + "a" * 66)
    assert ran == []


# --- _run_bounded ---------------------------------------------------------

def test_run_bounded_returns_output():
    r = mu._run_bounded(["sh", "-c", "echo out; echo err >&2; exit 3"], timeout=5)
    assert (r.stdout.strip(), r.stderr.strip(), r.returncode) == ("out", "err", 3)


def test_run_bounded_times_out_and_kills():
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        mu._run_bounded(["sleep", "30"], timeout=0.3)
    assert time.monotonic() - t0 < 5


def test_run_bounded_abandons_a_child_that_will_not_die(monkeypatch):
    """A makemkvcon blocked in the kernel ignores SIGKILL until its I/O ends.
    subprocess.run would wait for it forever; the caller must get control back."""
    reaped = threading.Event()

    class Unkillable:
        pid = 4242
        returncode = None

        def __init__(self, *a, **k):
            self.killed = False

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("makemkvcon", timeout)

        def kill(self):
            self.killed = True

        def wait(self, timeout=None):
            reaped.set()                            # the reaper thread is the only waiter
            return -9

    monkeypatch.setattr(mu.subprocess, "Popen", Unkillable)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        mu._run_bounded(["makemkvcon"], timeout=0.1, reap_grace=0.1)
    assert time.monotonic() - t0 < 2
    assert reaped.wait(2), "the stuck child must be handed to a reaper"


# --- _live_makemkvcon_running ---------------------------------------------

def _proc(root, pid, comm, *states):
    d = root / str(pid)
    (d / "task").mkdir(parents=True)
    (d / "comm").write_bytes(comm + b"\n")
    for i, st in enumerate(states):
        t = d / "task" / str(pid + i)
        t.mkdir()
        (t / "stat").write_bytes(f"{pid + i} ({comm.decode()}) {st} 1 2 3".encode())


def test_live_makemkvcon_detection(tmp_path, monkeypatch):
    monkeypatch.setattr(mu, "_PROC_ROOT", str(tmp_path))
    _proc(tmp_path, 10, b"python3", "S")
    _proc(tmp_path, 20, b"makemkvcon", "Z")               # fully dead zombie: drive is free
    assert mu._live_makemkvcon_running() is False
    _proc(tmp_path, 30, b"makemkvcon", "Z", "D")          # killed, one thread stuck on the drive
    assert mu._live_makemkvcon_running() is True


def test_running_makemkvcon_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(mu, "_PROC_ROOT", str(tmp_path))
    _proc(tmp_path, 40, b"makemkvcon", "S", "S")
    assert mu._live_makemkvcon_running() is True
