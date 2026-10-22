"""#911: eject the disc when its copy has finished and verified.

The removed ``eject_on_finish`` waited for the user's Finish, after labeling, post-process
and transfer. ``eject_after_copy`` frees the drive as soon as the rip is
verified, which is what hands-off ripping needs. It must never eject a disc
that is still needed (segment-reorder re-rips), a drive that is busy, or a
disc the user swapped in while verification ran.
"""
from __future__ import annotations

import threading
import uuid

import pytest
from fastapi.testclient import TestClient

from api import crud, models
from core import settings
from workers.tasks import _post_rip_verification_complete_callback


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "_SETTINGS_FILE", tmp_path / "settings.json")
    yield


class Ejects(list):
    def __init__(self, calls, done):
        super().__init__()
        self.calls, self.done = calls, done

    def __eq__(self, other):
        return self.calls == other


@pytest.fixture
def ejected(monkeypatch):
    """Record ejects synchronously (the real call runs on a thread)."""
    calls: list[str] = []
    done = threading.Event()

    def fake_eject(mount_point):
        calls.append(mount_point)
        done.set()
        return True

    monkeypatch.setattr("core.utils.eject_disc", fake_eject)
    monkeypatch.setattr("core.drive_registry._device_in_active_use", lambda dev: False)
    monkeypatch.setattr("core.disc_cache.get_by_mount_point", lambda mp: None)
    monkeypatch.setattr("workers.tasks.start_transfer.delay", lambda job_id: type("R", (), {"id": "x"})())
    return Ejects(calls, done)


@pytest.fixture
def job_id(test_db):
    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
            job_status="running", rip_state="running", rip_phase="verification",
            rip_progress=100, stage_profile="hit",
        )
        session.add(job)
        session.commit()
        yield str(job.id)
    finally:
        session.close()


def _verify(job_id):
    _post_rip_verification_complete_callback(job_id, success=True, ripped_files={"t1": "00001.mkv"})


def test_off_by_default(job_id, ejected):
    assert settings.load_settings().get("eject_after_copy") is False
    _verify(job_id)
    assert not ejected.done.wait(0.3)


def test_ejects_once_the_copy_is_verified(job_id, ejected, test_db):
    settings.save_settings({"eject_after_copy": True})
    _verify(job_id)
    assert ejected.done.wait(2)
    assert ejected == ["/dev/sr0"]
    with test_db() as db:
        assert crud.get_job(db, job_id).rip_state == "completed"


def test_heal_path_also_ejects(job_id, ejected):
    """Verification 'failed' but the rip is clearly done: the job completes,
    so the disc is no longer needed either."""
    settings.save_settings({"eject_after_copy": True})
    _post_rip_verification_complete_callback(
        job_id, success=False, error_reason="verification timed out", error_type="verification_failed",
    )
    assert ejected.done.wait(2)


def test_failed_rip_keeps_the_disc(test_db, ejected):
    settings.save_settings({"eject_after_copy": True})
    session = test_db()
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}")
    session.add(disc)
    session.flush()
    job = models.Job(disc_id=disc.id, disc_num="0", mount_point="/dev/sr0", job_status="running",
                     rip_state="running", rip_phase="verification", rip_progress=10, stage_profile="hit")
    session.add(job)
    session.commit()
    jid = str(job.id)
    session.close()
    _post_rip_verification_complete_callback(jid, success=False, error_reason="read error", error_type="x")
    assert not ejected.done.wait(0.3)


def test_segment_reorder_rerip_keeps_the_disc(job_id, ejected, monkeypatch):
    settings.save_settings({"eject_after_copy": True})
    monkeypatch.setattr("api.routers.jobs._maybe_dispatch_segment_previews", lambda job, jid: True)
    _verify(job_id)
    assert not ejected.done.wait(0.3)


def test_busy_drive_is_not_ejected(job_id, ejected, monkeypatch):
    settings.save_settings({"eject_after_copy": True})
    monkeypatch.setattr("core.drive_registry._device_in_active_use", lambda dev: True)
    _verify(job_id)
    assert not ejected.done.wait(0.3)


def test_a_swapped_in_disc_is_not_ejected(job_id, ejected, monkeypatch):
    settings.save_settings({"eject_after_copy": True})
    monkeypatch.setattr(
        "core.disc_cache.get_by_mount_point",
        lambda mp: {"mount_point": mp, "disc_id": str(uuid.uuid4())},
    )
    _verify(job_id)
    assert not ejected.done.wait(0.3)


def test_the_same_disc_still_in_the_drive_is_ejected(job_id, ejected, monkeypatch, test_db):
    settings.save_settings({"eject_after_copy": True})
    with test_db() as db:
        disc_id = str(crud.get_job(db, job_id).disc_id)
    monkeypatch.setattr(
        "core.disc_cache.get_by_mount_point", lambda mp: {"mount_point": mp, "disc_id": disc_id},
    )
    _verify(job_id)
    assert ejected.done.wait(2)


def test_setting_round_trips_through_the_api(test_db):
    from api.main import app

    client = TestClient(app)
    r = client.post("/system/discdb-lookup/config", json={
        "discdb_miss_workflow_with_prefill": True, "eject_after_copy": True,
    })
    assert r.status_code == 200, r.text
    assert r.json()["eject_after_copy"] is True
    assert client.get("/system/discdb-lookup/config").json()["eject_after_copy"] is True
    assert "eject_on_finish" not in settings.load_settings()


# --- eject_disc: kernel ioctl first (the image has no `eject` binary) ------

def test_eject_uses_the_kernel_ioctl(monkeypatch):
    """Prod 2026-10-19: eject-after-copy fired but logged "'eject' command not
    found" -- the image has no eject binary. The ioctl needs none."""
    from core import utils

    calls = []
    monkeypatch.setattr(utils.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(utils.os, "close", lambda fd: calls.append(("close", fd)))
    monkeypatch.setattr(utils.fcntl, "ioctl", lambda fd, req, *a: calls.append((fd, req, a)))
    monkeypatch.setattr(utils.shutil, "which", lambda name: None)
    assert utils.eject_disc("/dev/sr0") is True
    assert (7, utils.CDROM_LOCKDOOR, (0,)) in calls       # tray unlocked first
    assert (7, utils.CDROMEJECT, ()) in calls
    assert ("close", 7) in calls


def test_eject_falls_back_to_the_command_when_the_ioctl_fails(monkeypatch):
    import subprocess

    from core import utils

    def no_device(path, flags):
        raise FileNotFoundError(path)

    ran = []
    monkeypatch.setattr(utils.os, "open", no_device)
    monkeypatch.setattr(utils.shutil, "which", lambda name: "/usr/bin/eject")
    monkeypatch.setattr(utils, "run_bounded",
                        lambda cmd, *, timeout, **kw: ran.append(cmd) or subprocess.CompletedProcess(cmd, 0, "", ""))
    assert utils.eject_disc("/dev/sr0") is True
    assert ran == [["/usr/bin/eject", "/dev/sr0"]]


def test_eject_reports_failure_with_no_ioctl_and_no_command(monkeypatch):
    from core import utils

    monkeypatch.setattr(utils.os, "open", lambda path, flags: (_ for _ in ()).throw(PermissionError(path)))
    monkeypatch.setattr(utils.shutil, "which", lambda name: None)
    assert utils.eject_disc("/dev/sr0") is False


def test_eject_is_bounded_on_a_hung_drive(monkeypatch):
    """The ioctl blocks in the kernel on a hung drive; it must not hold its caller."""
    import time

    from core import utils

    release = threading.Event()
    monkeypatch.setattr(utils, "EJECT_TIMEOUT_SECONDS", 0.2)
    monkeypatch.setattr(utils.os, "open", lambda path, flags: 7)
    monkeypatch.setattr(utils.os, "close", lambda fd: None)
    monkeypatch.setattr(utils.fcntl, "ioctl", lambda fd, req, *a: release.wait(10))
    try:
        t0 = time.monotonic()
        assert utils.eject_disc("/dev/sr0") is False
        assert time.monotonic() - t0 < 2
    finally:
        release.set()


def test_eject_on_restart_reads_the_disc_list_from_the_right_module():
    """eject_on_restart imported get_cached_discs from core.disc_cache, where it
    doesn't exist, so the setting silently did nothing."""
    import inspect

    import api.main as main
    from core import disc_manager

    assert hasattr(disc_manager, "get_cached_discs")
    src = inspect.getsource(main)
    assert "from core.disc_cache import get_cached_discs" not in src
