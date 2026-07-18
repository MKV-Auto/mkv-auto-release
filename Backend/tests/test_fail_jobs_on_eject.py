"""
Tests for _fail_jobs_for_disc: revoke Celery task and mark job failed on disc ejection.
Also tests that ejection handler never attempts to mount discs.
"""
import uuid
from unittest.mock import Mock, patch

import pytest

from api import models
from api.routers.jobs import _fail_jobs_for_disc


@pytest.fixture
def disc_with_running_job(test_db):
    """Create a disc and a running job with celery_task_id for eject tests."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="test_disc_hash_eject",
            disc_number=1,
            format="BD",
        )
        session.add(disc)
        session.commit()
        session.refresh(disc)
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="running",
            celery_task_id="rip_disc:test_task_eject_123",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield disc, job, session


def test_fail_jobs_for_disc_revokes_celery_task_and_marks_failed(disc_with_running_job, monkeypatch):
    """_fail_jobs_for_disc revokes the job's Celery task and marks the job failed."""
    disc, job, session = disc_with_running_job
    revoke_calls = []

    def mock_revoke(task_id, terminate=False):
        revoke_calls.append((task_id, terminate))

    monkeypatch.setattr(
        "workers.tasks.celery_app.control.revoke",
        mock_revoke,
    )

    failed_ids = _fail_jobs_for_disc(
        disc_hash=disc.content_hash,
        db=session,
        reason="disc ejected",
    )

    assert failed_ids == [str(job.id)]
    assert len(revoke_calls) == 1
    assert revoke_calls[0][0] == "rip_disc:test_task_eject_123"
    assert revoke_calls[0][1] is True

    session.refresh(job)
    assert job.job_status == "failed"
    assert job.rip_state == "failed"
    assert "disc ejected" in (job.error_reason or "")


def test_fail_jobs_for_disc_kills_makemkvcon_for_disc(disc_with_running_job, monkeypatch):
    """_fail_jobs_for_disc calls kill_makemkvcon_for_disc with the job's mount_point (or disc_num)."""
    disc, job, session = disc_with_running_job
    kill_calls = []

    def mock_revoke(task_id, terminate=False):
        pass

    def mock_kill_makemkvcon(disc_num_or_mount: str, sigterm_timeout_seconds: float = 3.0) -> bool:
        kill_calls.append((disc_num_or_mount, sigterm_timeout_seconds))
        return True

    monkeypatch.setattr("workers.tasks.celery_app.control.revoke", mock_revoke)
    monkeypatch.setattr("core.utils.kill_makemkvcon_for_disc", mock_kill_makemkvcon)

    failed_ids = _fail_jobs_for_disc(
        disc_hash=disc.content_hash,
        db=session,
        reason="disc ejected",
    )

    assert failed_ids == [str(job.id)]
    assert len(kill_calls) == 1
    # Code uses mount_point first, then disc_num; job has mount_point="/dev/sr0"
    assert kill_calls[0][0] == "/dev/sr0"


def test_fail_jobs_for_disc_no_op_when_no_disc_hash(test_db):
    """_fail_jobs_for_disc returns empty when neither disc_hash nor mount_point is given."""
    with test_db() as session:
        failed_ids = _fail_jobs_for_disc(disc_hash=None, db=session, reason="disc ejected")
    assert failed_ids == []


def test_fail_jobs_for_disc_by_mount_point_without_hash(disc_with_running_job, monkeypatch):
    """When disc_hash is unknown, fail active jobs on that mount_point only."""
    disc, job, session = disc_with_running_job
    revoke_calls = []

    def mock_revoke(task_id, terminate=False):
        revoke_calls.append((task_id, terminate))

    monkeypatch.setattr("workers.tasks.celery_app.control.revoke", mock_revoke)

    failed_ids = _fail_jobs_for_disc(
        db=session,
        reason="disc ejected",
        mount_point="/dev/sr0",
    )
    assert failed_ids == [str(job.id)]
    assert len(revoke_calls) == 1
    session.refresh(job)
    assert job.job_status == "failed"


def test_fail_jobs_for_disc_does_not_fail_job_when_rip_completed(test_db, monkeypatch):
    """_fail_jobs_for_disc does not select or fail jobs that are past rip (e.g. in label phase)."""
    with test_db() as session:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash="test_disc_hash_label_phase",
            disc_number=1,
            format="BD",
        )
        session.add(disc)
        session.commit()
        session.refresh(disc)
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/dev/sr0",
            job_status="running",
            rip_state="completed",  # Past rip; in label phase
            celery_task_id=None,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)
        disc_hash = disc.content_hash
        original_status = job.job_status
        original_rip_state = job.rip_state

    revoke_calls = []
    monkeypatch.setattr(
        "workers.tasks.celery_app.control.revoke",
        lambda task_id, terminate=False: revoke_calls.append((task_id, terminate)),
    )

    with test_db() as session:
        failed_ids = _fail_jobs_for_disc(
            disc_hash=disc_hash,
            db=session,
            reason="disc ejected",
        )

    assert failed_ids == []
    assert len(revoke_calls) == 0

    with test_db() as session:
        job_after = session.query(models.Job).filter(models.Job.id == job_id).one()
        assert job_after.job_status == original_status
        assert job_after.rip_state == original_rip_state
        assert job_after.error_reason is None


def test_fail_jobs_for_disc_job_without_celery_task_id(disc_with_running_job, monkeypatch):
    """When job has no celery_task_id, revoke is not called but job is still marked failed."""
    disc, job, session = disc_with_running_job
    job.celery_task_id = None
    session.commit()
    session.refresh(job)

    revoke_calls = []

    def mock_revoke(task_id, terminate=False):
        revoke_calls.append((task_id, terminate))

    monkeypatch.setattr(
        "workers.tasks.celery_app.control.revoke",
        mock_revoke,
    )

    failed_ids = _fail_jobs_for_disc(
        disc_hash=disc.content_hash,
        db=session,
        reason="disc ejected",
    )

    assert failed_ids == [str(job.id)]
    assert len(revoke_calls) == 0
    session.refresh(job)
    assert job.job_status == "failed"
    assert job.rip_state == "failed"


def test_handle_disc_eject_for_device_resolves_hash_when_udev_num_differs(monkeypatch):
    """
    Cache keyed by MakeMKV index (e.g. '0') at /dev/sr2; udev sends disc_num '2'.
    Eject by device must still return disc_hash for job failure.
    """
    from core import disc_cache
    from core._drive_operations import handle_disc_eject_for_device

    disc_cache.clear()
    disc_cache.set_payload(
        "0",
        {
            "disc_num": "0",
            "mount_point": "/dev/sr2",
            "disc_hash": "hash_from_slot_zero",
            "content_hash": "hash_from_slot_zero",
        },
    )

    monkeypatch.setattr("core.utils.kill_makemkvcon_for_disc", lambda *a, **k: False)
    monkeypatch.setattr("core.utils._find_makemkvcon_process_for_disc", lambda *a: (None, None))

    result = handle_disc_eject_for_device("/dev/sr2", udev_disc_num="2")
    assert result.get("disc_hash") == "hash_from_slot_zero"
    assert disc_cache.get("0") is None


def test_kill_makemkvcon_for_disc_sends_sigkill(monkeypatch):
    """kill_makemkvcon_for_disc finds process and sends SIGKILL immediately (no SIGTERM wait)."""
    import signal
    from core.utils import kill_makemkvcon_for_disc

    kill_calls = []

    def mock_find(disc_num_or_mount: str):
        return (999, "makemkvcon mkv dev:1 ...")

    def mock_kill(pid, sig):
        kill_calls.append((pid, sig))

    monkeypatch.setattr("core.utils._find_makemkvcon_process_for_disc", mock_find)
    monkeypatch.setattr("os.kill", mock_kill)

    result = kill_makemkvcon_for_disc("1")

    assert result is True
    assert kill_calls == [(999, signal.SIGKILL)]


def test_kill_makemkvcon_for_disc_returns_false_when_no_process(monkeypatch):
    """kill_makemkvcon_for_disc returns False when no makemkvcon process is found."""
    from core.utils import kill_makemkvcon_for_disc

    monkeypatch.setattr("core.utils._find_makemkvcon_process_for_disc", lambda _: (None, None))

    result = kill_makemkvcon_for_disc("1")

    assert result is False
