"""Automated cleanup must never delete a job's ONLY copy of the rip.

Prod incident (2026-09-01): a UHD job failed in post-process (transfer never
ran), the container restarted for a release, and _startup_cleanup_terminal_jobs
enqueued cleanup_job_mkv which deleted the 48GB raw/ rip — the sole copy.
Log forensics found four earlier jobs eaten the same way at prior boots.
"""
import uuid
from types import SimpleNamespace

from core.job_cleanup import job_source_is_safe_to_clean


def _job(status, transfer):
    return SimpleNamespace(job_status=status, transfer_state=transfer)


def test_failed_before_transfer_is_never_cleanable():
    # The incident shape: post-process failed, transfer never ran.
    assert not job_source_is_safe_to_clean(_job("failed", "pending"))
    assert not job_source_is_safe_to_clean(_job("failed", None))
    assert not job_source_is_safe_to_clean(_job("failed", "failed"))
    assert not job_source_is_safe_to_clean(_job("failed", "running"))


def test_transferred_or_completed_jobs_are_cleanable():
    assert job_source_is_safe_to_clean(_job("failed", "completed"))
    assert job_source_is_safe_to_clean(_job("completed", "completed"))
    assert job_source_is_safe_to_clean(_job("completed", None))


def test_cleanup_task_refuses_unsafe_job(test_db, tmp_path, monkeypatch):
    """cleanup_job_mkv is the last line of defense: even if a caller enqueues
    an unsafe job (old code, manual call), it refuses and leaves the files."""
    from api import models
    from workers import tasks as worker_tasks

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"cl-{uuid.uuid4().hex[:8]}")
        session.add(disc)
        session.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
                         mount_point="/dev/sr0", job_status="failed",
                         transfer_state="pending", transfer_source_cleaned=False)
        session.add(job)
        session.commit()
        job_id = str(job.id)
    finally:
        session.close()

    raw = tmp_path / "data" / "jobs" / job_id / "raw"
    raw.mkdir(parents=True)
    mkv = raw / "title_t00.mkv"
    mkv.write_bytes(b"x" * 1024)
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(worker_tasks, "DATA_ROOT", tmp_path / "data", raising=False)

    worker_tasks.cleanup_job_mkv.run(job_id, "startup_cleanup")

    assert mkv.exists(), "the only copy of the rip must survive automated cleanup"
    session = test_db()
    try:
        refreshed = session.get(models.Job, job_id)
        assert refreshed.transfer_source_cleaned is False
    finally:
        session.close()


def test_cleanup_task_still_cleans_transferred_job(test_db, tmp_path, monkeypatch):
    from api import models
    from workers import tasks as worker_tasks

    session = test_db()
    try:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash=f"cl2-{uuid.uuid4().hex[:8]}")
        session.add(disc)
        session.flush()
        job = models.Job(id=str(uuid.uuid4()), disc_id=disc.id, disc_num="0",
                         mount_point="/dev/sr0", job_status="failed",
                         transfer_state="completed", transfer_source_cleaned=False)
        session.add(job)
        session.commit()
        job_id = str(job.id)
    finally:
        session.close()

    raw = tmp_path / "data" / "jobs" / job_id / "raw"
    raw.mkdir(parents=True)
    mkv = raw / "title_t00.mkv"
    mkv.write_bytes(b"x" * 1024)
    monkeypatch.setenv("MKVAUTO_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(worker_tasks, "DATA_ROOT", tmp_path / "data", raising=False)

    worker_tasks.cleanup_job_mkv.run(job_id, "startup_cleanup")

    assert not mkv.exists(), "a transferred job's source is cleanable"
    session = test_db()
    try:
        refreshed = session.get(models.Job, job_id)
        assert refreshed.transfer_source_cleaned is True
    finally:
        session.close()
