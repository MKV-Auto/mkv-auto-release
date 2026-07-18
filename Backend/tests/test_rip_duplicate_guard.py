"""
Tests for rip duplicate/failure guards: no rmtree on job path, and completed rip not overwritten by failure.
Also tests that early worker failures (e.g. lock fail) report via rip-complete callback.
"""
import uuid
from unittest.mock import patch, MagicMock

import pytest

from api import crud, models
from workers.tasks import JobTask, rip_disc

pytestmark = pytest.mark.integration


@pytest.fixture
def job_completed_rip(test_db):
    """Job with rip_state=completed (idempotent return path)."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="guard-hash", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="completed",
            phase="label",
            transfer_state="pending",
            stage_profile="miss",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield job.id


def test_rip_disc_does_not_call_rmtree_on_job_path(test_db, job_completed_rip):
    """rip_disc must not call shutil.rmtree on the job root (ensure_layout only, no wipe)."""
    import shutil
    rmtree_calls = []

    def spy_rmtree(path, *args, **kwargs):
        rmtree_calls.append(path)

    with pytest.MonkeyPatch().context() as m:
        m.setattr(shutil, "rmtree", spy_rmtree)
        with test_db() as session:
            job = crud.get_job(session, str(job_completed_rip))
            assert job is not None
            rip_disc(str(job.id), job.disc_num, job.mount_point, "copy", getattr(job, "output_dir", None) or "/tmp/out")
            assert not rmtree_calls, "rmtree should not be called when rip_state is completed (idempotent)"


def test_set_status_does_not_overwrite_completed_rip_with_failure(test_db):
    """When set_status would set job_status=failed and rip_state=failed but rip is clearly done, those fields are stripped."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="guard2", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="completed",
            rip_progress=100,
            phase="postprocess",
            transfer_state="pending",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)

    with test_db() as session:
        job = crud.get_job(session, job_id)
        assert job.rip_state == "completed"
        assert job.job_status == "running"
        task = JobTask()
        task.set_status(job, session, job_status="failed", rip_state="failed", error_reason="late failure report")
        session.commit()
        session.refresh(job)
        # Guard should have stripped failure fields so rip remains completed
        assert job.rip_state == "completed"
        assert job.job_status == "running"


def test_set_status_does_not_overwrite_rip_progress_100_with_failure(test_db):
    """When job has rip_progress>=100 and set_status would set failed, job is not marked failed."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="guard3", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="running",
            rip_progress=100,
            phase="rip",
            transfer_state="pending",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)

    with test_db() as session:
        job = crud.get_job(session, job_id)
        task = JobTask()
        task.set_status(job, session, job_status="failed", rip_state="failed", error_reason="duplicate failure")
        session.commit()
        session.refresh(job)
        assert job.rip_state != "failed"
        assert job.job_status != "failed"


def test_set_status_does_not_overwrite_non_empty_ripped_files_with_failure(test_db):
    """When job has non-empty ripped_files and set_status would set failed, job is not marked failed."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="guard4", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="running",
            rip_progress=50,
            phase="rip",
            transfer_state="pending",
            ripped_files={"title-uuid": "out.mkv"},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)

    with test_db() as session:
        job = crud.get_job(session, job_id)
        task = JobTask()
        task.set_status(job, session, job_status="failed", rip_state="failed", error_reason="late failure")
        session.commit()
        session.refresh(job)
        assert job.rip_state != "failed"
        assert job.job_status != "failed"


@pytest.fixture
def job_running_rip(test_db):
    """Job with rip_state=running (so worker proceeds until lock check)."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="early-fail-hash", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="running",
            phase="rip",
            transfer_state="pending",
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield job.id


def test_rip_disc_lock_fail_calls_rip_complete_callback_with_failure(test_db, job_running_rip):
    """When rip_disc cannot acquire the rip lock (drive busy), it calls rip-complete callback with success=False."""
    with test_db() as session:
        job = crud.get_job(session, str(job_running_rip))
        assert job is not None
        job_id = str(job.id)
        disc_num = job.disc_num
        mount_point = job.mount_point
    with patch("workers.tasks.acquire_operation_lock", return_value=None):
        with patch("workers.tasks._post_rip_complete_callback", new_callable=MagicMock) as mock_callback:
            # So we reach the lock check instead of "duplicate task" early return
            with patch("core.drive_gatekeeper.is_rip_running_for_disc", return_value=(False, None)):
                rip_disc(job_id, disc_num, mount_point, "copy", "/tmp/out")
    mock_callback.assert_called_once()
    # _post_rip_complete_callback(job_id, success=False, error_reason=msg)
    call_args, call_kw = mock_callback.call_args
    assert call_args[0] == job_id
    assert call_kw.get("success") is False
    assert "Drive busy" in (call_kw.get("error_reason") or "")
    assert call_kw.get("error_type") == "drive_busy"
    assert isinstance(call_kw.get("debug"), dict)
    assert "lock_files" in (call_kw.get("debug") or {})
