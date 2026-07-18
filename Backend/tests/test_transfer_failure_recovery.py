"""
Tests for transfer failure recovery: transfer failure should NOT fail the job.

When transfer fails, only transfer_state is set to 'failed'; the overall
job_status stays 'running' so the job remains visible in the UI and the user
can fix the destination and retry without re-ripping.
"""
import uuid
from unittest.mock import Mock, patch

import pytest

from core.job_state import StageState, apply_job_state


class TestStageStateTransferFailed:
    """StageState.transfer_failed() should NOT set job_status to failed."""

    @pytest.fixture
    def job_with_transfer(self, test_db, tmp_path):
        """Create a running job with completed post-process, ready for transfer."""
        from api import models

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="transfer_failure_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)

            job_id = str(uuid.uuid4())
            job = models.Job(
                id=job_id,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="running",
                rip_state="completed",
                transfer_state="running",
                rip_progress=100,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            yield job, session

    def test_transfer_failed_keeps_job_running(self, job_with_transfer):
        """Transfer failure should set transfer_state=failed but job_status stays running."""
        job, session = job_with_transfer

        StageState.transfer_failed(
            session,
            job,
            error_reason="NAS unreachable",
            reason="test transfer failure",
        )

        assert job.transfer_state == "failed"
        assert job.transfer_error == "NAS unreachable"
        assert job.job_status == "running", \
            "job_status should stay 'running' after transfer failure (not 'failed')"

    def test_transfer_failed_job_visible_in_unfinished_query(self, job_with_transfer):
        """Jobs with failed transfer should still appear in unfinished job queries."""
        from api import models as db_models

        job, session = job_with_transfer

        StageState.transfer_failed(
            session,
            job,
            error_reason="Connection refused",
        )

        # The unfinished query filters: rip_state IN (completed, skipped), job_status IN (running, validating)
        unfinished = (
            session.query(db_models.Job)
            .filter(
                db_models.Job.rip_state.in_(["completed", "skipped"]),
                db_models.Job.job_status.in_(["running", "validating"]),
            )
            .all()
        )
        job_ids = [str(j.id) for j in unfinished]
        assert str(job.id) in job_ids, \
            "Job with failed transfer should appear in unfinished query (job_status=running)"

    def test_transfer_failed_not_cleaned_by_startup_cleanup(self, job_with_transfer, monkeypatch):
        """Jobs with failed transfer should NOT be picked up by startup cleanup (not terminal)."""
        from api import models as db_models

        job, session = job_with_transfer

        StageState.transfer_failed(
            session,
            job,
            error_reason="disk full",
        )
        session.close()

        enqueued = []
        def mock_delay(job_id, reason):
            enqueued.append((job_id, reason))

        import workers.tasks as tasks_module
        monkeypatch.setattr(tasks_module.cleanup_job_mkv, "delay", mock_delay)

        from api.main import _startup_cleanup_terminal_jobs
        _startup_cleanup_terminal_jobs()

        enqueued_ids = {jid for jid, _ in enqueued}
        assert str(job.id) not in enqueued_ids, \
            "Transfer-failed job (job_status=running) should NOT be cleaned up on startup"


class TestRetryTransfer:
    """User-initiated retry should reset retry counter and allow fresh attempts."""

    @pytest.fixture
    def failed_transfer_job(self, test_db, tmp_path):
        """Create a job with failed transfer."""
        from api import models

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="retry_disc",
                disc_number=1,
                format="BD",
            )
            session.add(disc)
            session.commit()
            session.refresh(disc)

            job_id = str(uuid.uuid4())
            job = models.Job(
                id=job_id,
                disc_id=disc.id,
                disc_num="1",
                mount_point="/dev/sr0",
                job_status="running",
                rip_state="completed",
                transfer_state="failed",
                transfer_error="NAS unreachable",
                transfer_retry_count=1,
                rip_progress=100,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            yield job, session

    def test_retry_resets_counter(self, failed_transfer_job):
        """User-initiated retry should reset transfer_retry_count to 0."""
        from core.transfer.utils.error_handler import retry_transfer

        job, session = failed_transfer_job

        retry_transfer(session, str(job.id))

        assert job.transfer_state == "pending"
        assert job.transfer_error is None
        assert job.transfer_progress == 0
        assert job.transfer_retry_count == 0, \
            "User-initiated retry should reset retry count to 0"

    def test_retry_rejects_non_failed_state(self, test_db):
        """Retry should reject if transfer is not in a retryable state."""
        from api import models
        from core.transfer.utils.error_handler import retry_transfer

        with test_db() as session:
            disc = models.Disc(
                id=str(uuid.uuid4()),
                content_hash="retry_reject_disc",
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
                transfer_state="running",
            )
            session.add(job)
            session.commit()
            session.refresh(job)

            with pytest.raises(ValueError, match="not in a retryable state"):
                retry_transfer(session, str(job.id))
