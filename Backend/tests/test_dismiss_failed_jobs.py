"""#877: failed jobs can be dismissed, and what a failed copy left behind expires.

Failed jobs were excluded from every cleanup (their raw/ may be the only copy
of a rip), so their files stayed forever: 75 GB on prod in October 2026. The
✕ on a failed card hides the job and, if the user says so, deletes its files.
Files from a copy that never finished also go after a retention period.
"""
from __future__ import annotations

import datetime as dt
import uuid

import pytest
from fastapi.testclient import TestClient

from api import models


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    from core import settings

    monkeypatch.setattr(settings, "_SETTINGS_FILE", tmp_path / "settings.json")


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    from workers import tasks as worker_tasks

    root = tmp_path / "data"
    monkeypatch.setenv("MKVAUTO_DATA", str(root))
    monkeypatch.setattr(worker_tasks, "DATA_ROOT", root, raising=False)
    return root


@pytest.fixture
def events(monkeypatch):
    """Coordinator events, and cleanup run inline instead of on Celery."""
    from api.routers import jobs as jobs_router
    from core import card_state
    from workers import tasks as worker_tasks

    sent = []
    monkeypatch.setattr(card_state, "publish_coordinator_event", lambda t, p: sent.append((t, p)))
    monkeypatch.setattr(jobs_router.cleanup_job_mkv, "delay",
                        lambda job_id, reason: worker_tasks.cleanup_job_mkv.run(job_id, reason))
    return sent


def _disc(db):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"dm-{uuid.uuid4().hex[:8]}", format="Blu-Ray")
    db.add(disc)
    db.flush()
    return disc


def _job(db, disc, *, status="failed", rip="failed", minutes=0, days_old=0):
    now = dt.datetime.now(dt.timezone.utc)
    job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
                     job_status=status, rip_state=rip, transfer_state="pending",
                     created_at=now - dt.timedelta(days=days_old) + dt.timedelta(minutes=minutes))
    db.add(job)
    db.flush()
    return job


def _rip_file(root, job_id, size=1024):
    raw = root / "jobs" / job_id / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    mkv = raw / "title_t00.mkv"
    mkv.write_bytes(b"x" * size)
    return mkv


# --- the endpoint ------------------------------------------------------------

def test_dismiss_hides_every_failed_job_for_the_disc_and_keeps_files(test_db, data_root, events):
    """The carousel shows the newest failure per disc; dismissing only that
    one would surface the next-older failure as a fresh card."""
    from api.main import app
    from api.unfinished_jobs import query_unfinished_jobs

    with test_db() as db:
        disc = _disc(db)
        old, new = _job(db, disc, minutes=0), _job(db, disc, minutes=5)
        db.commit()
        ids, disc_id = [new.id, old.id], disc.id
    files = [_rip_file(data_root, i) for i in ids]

    r = TestClient(app).post(f"/jobs/{ids[0]}/dismiss", json={})
    assert r.status_code == 200, r.text
    assert set(r.json()["job_ids"]) == set(ids)
    assert all(f.exists() for f in files), "dismiss without delete keeps the files"
    with test_db() as db:
        assert all(db.get(models.Job, i).dismissed for i in ids)
        assert not [j for j in query_unfinished_jobs(db) if j.id in ids]
    assert events == [("jobs_dismissed", {"job_ids": ids, "disc_id": disc_id})]


def test_dismiss_with_delete_removes_the_files(test_db, data_root, events):
    from api.main import app

    with test_db() as db:
        job = _job(db, _disc(db))
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id)

    r = TestClient(app).post(f"/jobs/{job_id}/dismiss", json={"delete_files": True})
    assert r.status_code == 200, r.text
    assert not mkv.exists()
    with test_db() as db:
        assert db.get(models.Job, job_id).transfer_source_cleaned is True


def test_the_user_may_delete_a_finished_rip_automated_cleanup_keeps(test_db, data_root, events):
    """A finished rip whose post-processing failed is the only copy. The
    preview says so; if the user still chooses delete, it goes."""
    from api.main import app

    with test_db() as db:
        job = _job(db, _disc(db), rip="completed")
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id, size=4096)
    client = TestClient(app)

    preview = client.get(f"/jobs/{job_id}/dismiss").json()
    assert preview["has_finished_rip"] is True
    assert preview["bytes"] == 4096
    assert preview["retention_days"] == 7

    client.post(f"/jobs/{job_id}/dismiss", json={"delete_files": True})
    assert not mkv.exists()


def test_only_a_failed_job_can_be_dismissed(test_db, data_root, events):
    from api.main import app

    with test_db() as db:
        job = _job(db, _disc(db), status="running", rip="completed")
        db.commit()
        job_id = job.id
    client = TestClient(app)
    assert client.get(f"/jobs/{job_id}/dismiss").status_code == 409
    assert client.post(f"/jobs/{job_id}/dismiss", json={}).status_code == 409
    assert client.post(f"/jobs/{uuid.uuid4()}/dismiss", json={}).status_code == 404


def test_a_dismissed_failure_no_longer_marks_the_disc_in_the_drive(test_db, data_root, events):
    from api.routers.websockets import _resolve_in_drive_job_for_disc

    with test_db() as db:
        disc = _disc(db)
        job = _job(db, disc)
        db.commit()
        assert _resolve_in_drive_job_for_disc(db, disc.id)[1] == "failed"
        job.dismissed = True
        db.commit()
        assert _resolve_in_drive_job_for_disc(db, disc.id) == (None, None, None)


# --- expiry ------------------------------------------------------------------

def _expire(test_db):
    from workers import tasks as worker_tasks

    with test_db() as db:
        return worker_tasks._expire_failed_rips(db)


def test_an_old_unfinished_copy_is_deleted_and_dismissed(test_db, data_root, events):
    with test_db() as db:
        job = _job(db, _disc(db), days_old=8)
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id)

    assert _expire(test_db) == 1
    assert not mkv.exists()
    with test_db() as db:
        j = db.get(models.Job, job_id)
        assert j.dismissed and j.transfer_source_cleaned


def test_recent_failures_and_finished_rips_are_kept(test_db, data_root, events):
    with test_db() as db:
        recent = _job(db, _disc(db), days_old=2)
        finished = _job(db, _disc(db), rip="completed", days_old=30)   # the only copy
        db.commit()
        ids = [recent.id, finished.id]
    files = [_rip_file(data_root, i) for i in ids]

    assert _expire(test_db) == 0
    assert all(f.exists() for f in files)


def test_keeping_the_files_on_dismiss_does_not_restart_the_clock(test_db, data_root, events):
    from api.main import app

    with test_db() as db:
        job = _job(db, _disc(db), days_old=8)
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id)
    TestClient(app).post(f"/jobs/{job_id}/dismiss", json={"delete_files": False})
    assert mkv.exists()
    assert _expire(test_db) == 1
    assert not mkv.exists()


def test_zero_days_keeps_failed_copies(test_db, data_root, events):
    from core import settings

    settings.save_settings({"failed_rip_retention_days": 0})
    with test_db() as db:
        job = _job(db, _disc(db), days_old=400)
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id)
    assert _expire(test_db) == 0
    assert mkv.exists()


def test_automated_cleanup_still_refuses_a_failed_job(test_db, data_root):
    """The expiry reason is the only automated path into a failed job."""
    from workers import tasks as worker_tasks

    with test_db() as db:
        job = _job(db, _disc(db))
        db.commit()
        job_id = job.id
    mkv = _rip_file(data_root, job_id)
    worker_tasks.cleanup_job_mkv.run(job_id, "stale_cleanup")
    assert mkv.exists()
    worker_tasks.cleanup_job_mkv.run(job_id, "failed_rip_expired")
    assert not mkv.exists()


def test_the_retention_setting_round_trips():
    from api.main import app

    client = TestClient(app)
    body = client.get("/system/discdb-lookup/config").json()
    assert body["failed_rip_retention_days"] == 7
    body["failed_rip_retention_days"] = 14
    assert client.post("/system/discdb-lookup/config", json=body).json()["failed_rip_retention_days"] == 14
    body["failed_rip_retention_days"] = -1
    assert client.post("/system/discdb-lookup/config", json=body).status_code == 422
