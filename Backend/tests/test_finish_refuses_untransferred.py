"""#903: Finish, and the cleanup it triggers, never delete the only copy of
a labeled title that was never transferred.

Prod 2026-10-13: five jobs reported a completed transfer that had carried
only the backdrop; the user pressed Finish, and ``cleanup_job_mkv`` removed
each 42 GB main feature from raw/. Finish must refuse with the reason, and
the cleanup must refuse even if called directly.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

from fastapi.testclient import TestClient

from api import models
from api.main import app
from core.job_cleanup import untransferred_kept_titles


def _raw(tmp_path, monkeypatch, job_id, *names):
    """The job's raw/ dir under a temp data root, holding ``names``."""
    monkeypatch.setattr("core.job_paths.resolve_jobs_root", lambda _out_dir=None: tmp_path / "data" / "jobs")
    raw = tmp_path / "data" / "jobs" / job_id / "raw"
    raw.mkdir(parents=True, exist_ok=True)
    for n in names:
        (raw / n).write_bytes(b"x" * 2048)
    return raw


def _job(session, *, post_paths):
    disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"fin-{uuid.uuid4().hex[:8]}")
    session.add(disc)
    session.flush()
    main = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=10, source_file="00850.mpls",
                            title="Star Wars", type="MainMovie")
    backdrop = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=1, source_file="00020.mpls",
                                title="Backdrop", type="Backdrop")
    stub = models.DiscTitle(id=str(uuid.uuid4()), disc_id=disc.id, index=0, source_file="00650.mpls",
                            type="ignore")
    session.add_all([main, backdrop, stub])
    session.flush()
    ripped = {main.id: "A New Hope_t02.mkv", backdrop.id: "A New Hope_t01.mkv", stub.id: "A New Hope_t00.mkv"}
    paths = {k: v for k, v in {
        backdrop.id: "Movies/Star Wars (1977)/Other/Backdrop.1080p.mkv",
        main.id: "Movies/Star Wars (1977)/Star Wars.1080p.mkv",
    }.items() if k in post_paths(main.id, backdrop.id)}
    job = models.Job(
        id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0", mount_point="/dev/sr0",
        job_status="running", phase="complete", scan_state="completed", rip_state="completed",
        label_state="completed", transfer_state="completed", finalize_state="completed",
        finalize_release_state="completed", stage_profile="miss",
        ripped_files=ripped, post_paths=paths, disc_payload={"post_paths": paths},
    )
    session.add(job)
    session.commit()
    return job.id, main.id, backdrop.id


def test_helper_lists_kept_titles_whose_rip_is_still_in_raw(test_db, tmp_path, monkeypatch):
    with test_db() as db:
        job_id, main_id, _ = _job(db, post_paths=lambda main, backdrop: {backdrop})
        _raw(tmp_path, monkeypatch, job_id, "A New Hope_t02.mkv", "A New Hope_t00.mkv")
        job = db.get(models.Job, job_id)
        missing = untransferred_kept_titles(job, db)
    assert [m["title_id"] for m in missing] == [main_id]
    assert missing[0]["file"] == "A New Hope_t02.mkv"           # the ignored stub isn't listed


def test_helper_ignores_a_title_whose_file_left_raw_without_a_recorded_path(test_db, tmp_path, monkeypatch):
    """A legacy rename moved the file but recorded nothing: not a loss."""
    with test_db() as db:
        job_id, _, _ = _job(db, post_paths=lambda main, backdrop: {backdrop})
        _raw(tmp_path, monkeypatch, job_id)                      # raw/ is empty
        assert untransferred_kept_titles(db.get(models.Job, job_id), db) == []


def test_finish_refuses_while_a_kept_title_was_never_transferred(test_db, tmp_path, monkeypatch):
    with test_db() as db:
        job_id, _, _ = _job(db, post_paths=lambda main, backdrop: {backdrop})
    _raw(tmp_path, monkeypatch, job_id, "A New Hope_t02.mkv")
    with patch("api.routers.jobs.cleanup_job_mkv") as cleanup:
        r = TestClient(app).post(f"/jobs/{job_id}/finish")
    assert r.status_code == 409, r.text
    assert "Star Wars (A New Hope_t02.mkv)" in r.json()["detail"]
    assert "delete the only copy" in r.json()["detail"]
    cleanup.delay.assert_not_called()
    with test_db() as db:
        assert db.get(models.Job, job_id).job_status == "running"


def test_finish_proceeds_once_every_kept_title_has_a_path(test_db, tmp_path, monkeypatch):
    with test_db() as db:
        job_id, _, _ = _job(db, post_paths=lambda main, backdrop: {main, backdrop})
    _raw(tmp_path, monkeypatch, job_id, "A New Hope_t02.mkv")   # still in raw/, but it has a path
    with patch("api.routers.jobs.cleanup_job_mkv") as cleanup:
        r = TestClient(app).post(f"/jobs/{job_id}/finish")
    assert r.status_code in (200, 204), r.text
    with test_db() as db:
        assert db.get(models.Job, job_id).job_status == "completed"


def test_user_finish_cleanup_refuses_to_delete_an_untransferred_rip(test_db, tmp_path, monkeypatch):
    from workers import tasks as worker_tasks

    with test_db() as db:
        job_id, _, _ = _job(db, post_paths=lambda main, backdrop: {backdrop})
    raw = _raw(tmp_path, monkeypatch, job_id, "A New Hope_t02.mkv")
    feature = raw / "A New Hope_t02.mkv"
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(worker_tasks, "DATA_ROOT", tmp_path / "data", raising=False)

    worker_tasks.cleanup_job_mkv.run(job_id, "user_finish")

    assert feature.exists(), "user_finish must not delete a labeled title that was never transferred"
    with test_db() as db:
        assert db.get(models.Job, job_id).transfer_source_cleaned is False
