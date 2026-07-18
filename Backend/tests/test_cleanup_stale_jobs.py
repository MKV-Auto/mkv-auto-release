"""Tests for api.routers.jobs._cleanup_stale_jobs orphan detection."""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest

from api.routers import jobs as jobs_router


def _query_chain_maker(all_results: list[list]):
    """Return db.query side_effect that yields filter().all() chains in order."""
    idx = {"n": 0}

    def query(_model=None):
        i = idx["n"]
        idx["n"] += 1
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.all.return_value = all_results[i] if i < len(all_results) else []
        return chain

    return query


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
def test_completed_rip_not_added_to_orphan_jobs_on_revoked_celery_task(mock_async_result, monkeypatch):
    """REVOKED rip_disc handle after rip completed/skipped must not populate orphaned_jobs."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "celery-rip-task-id"
    job.rip_state = "completed"
    job.job_status = "running"
    job.post_state = "running"
    job.disc_payload = {}
    job.updated_at = None

    result_mock = MagicMock()
    result_mock.state = "REVOKED"
    mock_async_result.return_value = result_mock

    db = MagicMock()
    db.query.side_effect = _query_chain_maker(
        [
            [job],  # jobs_with_tasks
            [],  # stale_candidates
            [],  # stuck_postprocess
            [],  # jobs_with_previews
        ]
    )

    inspect_mock = MagicMock()
    # Empty worker map = inspect replied; None would skip orphan logic entirely
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock

    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    failed = jobs_router._cleanup_stale_jobs(db)
    assert failed == []


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
def test_running_rip_still_considered_orphan_when_task_revoked(mock_async_result, monkeypatch):
    """Jobs still ripping should remain orphan candidates when Celery task is not running."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "celery-rip-task-id"
    job.rip_state = "running"
    job.job_status = "running"
    job.post_state = "pending"
    job.disc_payload = {}
    job.updated_at = None
    # Newer orphan guards read these; a bare MagicMock attribute here either
    # raises inside is_pid_alive (swallowed by the per-job except, silently
    # skipping the orphan mark) or fakes a live pid. Pin them to the
    # no-live-process shape the scenario describes.
    job.rip_pid = None
    job.disc_num = "1"

    result_mock = MagicMock()
    result_mock.state = "REVOKED"
    mock_async_result.return_value = result_mock

    db = MagicMock()
    db.query.side_effect = _query_chain_maker(
        [
            [job],
            [],
            [],
            [],
        ]
    )

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock

    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "apply_job_state"
    ), patch.object(jobs_router, "_emit_disc_context_when_job_updates"), patch.object(
        jobs_router, "JobPaths"
    ) as jp, patch.object(jobs_router, "cleanup_job_mkv"):
        jp.for_id.return_value.root.exists.return_value = False
        jobs_router._cleanup_stale_jobs(db)
        stage_state.rip_failed.assert_called_once()


@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_marks_pending_when_no_worker_task(mock_async_result, monkeypatch):
    """PENDING in result backend must not protect a running rip if workers report no such task."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = None
    job.disc = MagicMock()
    job.disc.content_hash = "deadbeef"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "apply_job_state"
    ), patch.object(jobs_router, "_emit_disc_context_when_job_updates"), patch.object(
        jobs_router, "JobPaths"
    ) as jp, patch.object(jobs_router, "cleanup_job_mkv"):
        jp.for_id.return_value.root.exists.return_value = False
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert str(job.id) in failed_ids
        stage_state.rip_failed.assert_called_once()


@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_heals_when_copy_complete_in_db(
    mock_async_result, monkeypatch
):
    """Dead Celery task + rip_progress 100 → rip_copy_complete + rip_verification, not fail/cleanup."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = None
    job.rip_phase = None
    job.rip_progress = 100
    job.titles_completed = None
    job.total_titles = None
    job.ripped_files = {}
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "enqueue_rip_verification_for_job"
    ) as enqueue_verify, patch.object(
        jobs_router, "_emit_disc_context_when_job_updates"
    ), patch.object(jobs_router, "cleanup_job_mkv") as cleanup_mkv:
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_copy_complete.assert_called_once()
        stage_state.rip_failed.assert_not_called()
        enqueue_verify.assert_called_once()
        cleanup_mkv.delay.assert_not_called()


@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_heals_when_titles_complete_match(
    mock_async_result, monkeypatch
):
    """titles_completed >= total_titles triggers heal without requiring rip_progress 100."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = None
    job.rip_phase = "copy"
    job.rip_progress = 99
    job.titles_completed = 3
    job.total_titles = 3
    job.ripped_files = {}
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "enqueue_rip_verification_for_job"
    ) as enqueue_verify, patch.object(
        jobs_router, "_emit_disc_context_when_job_updates"
    ), patch.object(jobs_router, "cleanup_job_mkv"):
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_copy_complete.assert_called_once()
        stage_state.rip_failed.assert_not_called()
        enqueue_verify.assert_called_once()


@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_heals_verification_phase_without_second_copy_ack(
    mock_async_result, monkeypatch
):
    """rip_phase=verification + progress complete → only re-enqueue verify, no duplicate rip_copy_complete."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = None
    job.rip_phase = "verification"
    job.rip_progress = 100
    job.titles_completed = None
    job.total_titles = None
    job.ripped_files = {}
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "enqueue_rip_verification_for_job"
    ) as enqueue_verify, patch.object(
        jobs_router, "_emit_disc_context_when_job_updates"
    ), patch.object(jobs_router, "cleanup_job_mkv"):
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_copy_complete.assert_not_called()
        stage_state.rip_failed.assert_not_called()
        enqueue_verify.assert_called_once()


def test_fail_orphaned_rip_jobs_on_startup_skips_when_inspect_returns_none(monkeypatch):
    """If no worker responds to inspect, do not fail running rips (avoid false positives)."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = None
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_failed.assert_not_called()


@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_skips_when_task_on_worker(mock_async_result, monkeypatch):
    """Task id present on worker active list must not be failed."""
    tid = "rip_disc:live-task"
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = tid
    job.rip_pid = None
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": [{"id": tid, "name": "rip_disc"}]}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_failed.assert_not_called()


@patch.object(jobs_router, "is_pid_alive", return_value=True)
@patch("celery.result.AsyncResult")
def test_fail_orphaned_rip_jobs_on_startup_skips_when_rip_pid_alive(
    mock_async_result, _mock_pid_alive, monkeypatch
):
    """Live makemkv PID must prevent startup from failing the job."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.rip_pid = 12345
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_failed.assert_not_called()


@patch("core.disc_locks.is_operation_active", return_value=True)
def test_fail_orphaned_rip_jobs_on_startup_skips_when_rip_lock_held(mock_is_op, monkeypatch):
    """Startup recovery must not kill a running rip when the disc rip lock is held."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "rip_disc:test-job-id"
    job.disc_num = "0"
    job.disc_payload = {}
    job.rip_pid = None
    job.disc = MagicMock()
    job.disc.content_hash = "abc"

    db = MagicMock()
    q = MagicMock()
    q.join.return_value = q
    q.filter.return_value = q
    q.all.return_value = [job]
    db.query.return_value = q

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        failed_ids = jobs_router._fail_orphaned_rip_jobs_on_startup(db)
        assert failed_ids == []
        stage_state.rip_failed.assert_not_called()


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
def test_running_rip_orphan_when_pending_and_not_on_workers(mock_async_result, monkeypatch):
    """Stale cleanup: rip_state=running + PENDING backend + no worker task => orphan."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "celery-rip-task-id"
    job.rip_state = "running"
    job.job_status = "running"
    job.rip_pid = None
    job.post_state = "pending"
    job.disc_payload = {}
    job.updated_at = None

    result_mock = MagicMock()
    result_mock.state = "PENDING"
    mock_async_result.return_value = result_mock

    db = MagicMock()
    db.query.side_effect = _query_chain_maker(
        [
            [job],
            [],
            [],
            [],
        ]
    )

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state, patch.object(
        jobs_router, "apply_job_state"
    ), patch.object(jobs_router, "_emit_disc_context_when_job_updates"), patch.object(
        jobs_router, "JobPaths"
    ) as jp, patch.object(jobs_router, "cleanup_job_mkv"):
        jp.for_id.return_value.root.exists.return_value = False
        jobs_router._cleanup_stale_jobs(db)
        stage_state.rip_failed.assert_called_once()


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
@patch("core.disc_locks.is_operation_active", return_value=True)
def test_running_rip_not_orphan_when_rip_lock_held(mock_is_op_active, mock_async_result, monkeypatch):
    """Held OPERATION_RIP lock means rip_disc is active even if Celery inspect misses the task."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "celery-rip-task-id"
    job.rip_state = "running"
    job.job_status = "running"
    job.rip_pid = None
    job.disc_num = "0"
    job.disc_payload = {}
    job.post_state = "pending"
    job.updated_at = None

    result_mock = MagicMock()
    result_mock.state = "STARTED"
    mock_async_result.return_value = result_mock

    db = MagicMock()
    db.query.side_effect = _query_chain_maker(
        [
            [job],
            [],
            [],
            [],
        ]
    )

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        jobs_router._cleanup_stale_jobs(db)
        stage_state.rip_failed.assert_not_called()
    mock_is_op_active.assert_called()


@patch.object(jobs_router, "RIP_ORPHAN_INSPECT_GRACE_SECONDS", 600)
@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
def test_running_rip_not_orphan_started_within_grace(mock_async_result, monkeypatch):
    """STARTED + recent updated_at skips orphan when inspect is empty (grace window)."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = "celery-rip-task-id"
    job.rip_state = "running"
    job.job_status = "running"
    job.rip_pid = None
    job.disc_num = "1"
    job.disc_payload = {}
    job.post_state = "pending"
    job.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)

    result_mock = MagicMock()
    result_mock.state = "STARTED"
    mock_async_result.return_value = result_mock

    db = MagicMock()
    db.query.side_effect = _query_chain_maker(
        [
            [job],
            [],
            [],
            [],
        ]
    )

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "_rip_operation_lock_held_for_job", return_value=False), patch.object(
        jobs_router, "StageState"
    ) as stage_state:
        jobs_router._cleanup_stale_jobs(db)
        stage_state.rip_failed.assert_not_called()


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
@patch("celery.result.AsyncResult")
@patch("api.routers.jobs._get_drive_state", return_value=None)
@patch("api.routers.jobs.is_pid_alive", return_value=True)
def test_stale_no_progress_skips_when_rip_pid_alive(
    mock_pid_alive, mock_drive, mock_async_result, monkeypatch
):
    """Live rip_pid: worker owns output stall; API must not fail as 'timed out with no progress'."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = None
    job.rip_state = "running"
    job.job_status = "running"
    job.rip_pid = 5555
    job.post_state = "pending"
    job.disc_payload = {}
    job.rip_progress = 0
    job.transfer_state = None
    job.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=2000)

    db = MagicMock()
    db.query.side_effect = _query_chain_maker([[], [job], [], []])

    inspect_mock = MagicMock()
    inspect_mock.active.return_value = {"celery@test": []}
    inspect_mock.reserved.return_value = None
    inspect_mock.scheduled.return_value = None
    celery_app = MagicMock()
    celery_app.control.inspect.return_value = inspect_mock
    monkeypatch.setattr("workers.tasks.celery_app", celery_app, raising=False)

    with patch.object(jobs_router, "StageState") as stage_state:
        failed = jobs_router._cleanup_stale_jobs(db)
    assert failed == []
    stage_state.rip_failed.assert_not_called()
    mock_pid_alive.assert_called()


# ──────────────────────────────────────────────────────────────────────────
# Bucket-isolation regression: stuck-preview detection MUST NOT trigger the
# postprocess-reset action when the job isn't actually in stuck_postprocess.
#
# The bug: a job actively in postprocess (post_state="running", just
# transitioned by /label/complete or /postprocess) gets flagged for
# stuck-preview because of *prior* stale preview metadata. The action loop
# at jobs.py:1459 then fires the postprocess-reset because its only
# discriminator was ``post_state == "running" and job_status == "running"``
# — true for both buckets. Under devmode the reset forces job_status
# running→pending; the in-flight worker then crashes with
# ``StateViolation: Invalid job_status transition: pending -> validating``.
# Under production the same misclassification triggers attempt_recovery /
# postprocess_failed, also wrong.
#
# Fix: gate the postprocess-reset branch on ``stuck_postprocess_ids`` so
# the two buckets dispatch independently to their own actions.
# ──────────────────────────────────────────────────────────────────────────


def _preview_stuck_metadata(stale_seconds: int = 600) -> dict:
    """disc_payload that puts a job into the stuck_previews bucket."""
    stale_iso = (
        datetime.datetime.now(datetime.timezone.utc)
        - datetime.timedelta(seconds=stale_seconds)
    ).isoformat()
    return {"previews": {"status": "queued", "updated_at": stale_iso}}


def _make_job_in_postprocess(*, with_stale_previews: bool):
    """Job actively in postprocess (post_state=running). Optionally carries
    stale preview metadata that would land it in the stuck_previews bucket."""
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = None
    job.rip_state = "completed"
    job.job_status = "running"
    job.post_state = "running"
    # #365 — the post_state column is being dropped; the cleanup action
    # gate now reads job.derived_post_state. MagicMock doesn't run the
    # hybrid_property logic, so set the value explicitly to mirror what
    # the column says.
    job.derived_post_state = "running"
    job.disc_payload = _preview_stuck_metadata() if with_stale_previews else {}
    job.updated_at = datetime.datetime.now(datetime.timezone.utc)
    return job


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
def test_stuck_preview_with_postprocess_running_does_not_reset_postprocess(
    monkeypatch,
):
    """User-reported race: postprocess just started, stale preview metadata
    from earlier is still on the job. Previously the postprocess-reset
    action fired and forced job_status running→pending, crashing the
    in-flight worker on pending→validating. The fix: stuck-preview bucket
    must not trigger postprocess-reset."""
    job = _make_job_in_postprocess(with_stale_previews=True)

    # jobs_with_tasks empty, stale empty, stuck_postprocess empty
    # (the job is fresh — would not satisfy `updated_at < postprocess_cutoff`),
    # jobs_with_previews returns the job.
    db = MagicMock()
    db.query.side_effect = _query_chain_maker([[], [], [], [job]])

    # Devmode flips on so the buggy branch *would* call apply_job_state_devmode
    # if my fix regressed; production path also assertable but devmode is
    # what surfaced the bug in the wild.
    monkeypatch.setattr(jobs_router, "is_dev_mode", lambda: True)

    with patch.object(
        jobs_router, "apply_job_state_devmode"
    ) as buggy_reset, patch.object(
        jobs_router, "StageState"
    ) as stage_state, patch.object(
        jobs_router, "active_generate_previews_job_ids", return_value=set()
    ), patch.object(
        jobs_router, "build_preview_regeneration_state",
        return_value=({}, {}, "running"),
    ), patch.object(jobs_router, "generate_previews"):
        jobs_router._cleanup_stale_jobs(db)

    # The whole point: do NOT mutate post_state / job_status via the
    # devmode reset path. A future refactor that loses the bucket gate
    # would fail this loudly.
    buggy_reset.assert_not_called()
    # And: don't mark postprocess as failed via the production path
    # (StageState.postprocess_failed) either.
    stage_state.postprocess_failed.assert_not_called()


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
def test_stuck_postprocess_still_triggers_reset_under_devmode(monkeypatch):
    """Regression guard for the legitimate stuck-postprocess case: a job
    that's actually been in post_state="running" beyond the 600s timeout
    (i.e. came from the stuck_postprocess query, not stuck_previews) must
    still get the devmode reset so a developer can re-trigger."""
    job = _make_job_in_postprocess(with_stale_previews=False)
    # Old updated_at so the stuck_postprocess query would naturally include it.
    job.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=3600
    )

    db = MagicMock()
    # stuck_postprocess (query #3) returns the job; jobs_with_previews empty.
    db.query.side_effect = _query_chain_maker([[], [], [job], []])

    monkeypatch.setattr(jobs_router, "is_dev_mode", lambda: True)

    with patch.object(
        jobs_router, "apply_job_state_devmode"
    ) as devmode_reset, patch.object(jobs_router, "StageState"):
        jobs_router._cleanup_stale_jobs(db)

    devmode_reset.assert_called_once()
    # And it set the canonical reason so future bug-hunters can grep.
    call_kwargs = devmode_reset.call_args.kwargs
    assert call_kwargs.get("reason") == "stale post-process health check"
    # #365 step 5 — post_state column dropped; reset writes job_status only
    # and post_state is now derived via Job.derived_post_state.
    assert call_kwargs.get("updates", {}).get("job_status") == "pending"


@patch.object(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
def test_job_in_both_buckets_prefers_postprocess_reset(monkeypatch):
    """When a job is simultaneously stuck in postprocess AND has stale
    preview metadata, the postprocess-reset path wins (preserves the
    pre-fix prioritization — postprocess being stuck is the bigger
    concern). Verifies the bucket gate keeps the existing branch
    ordering for legitimately stuck postprocess jobs that also have
    stale previews."""
    job = _make_job_in_postprocess(with_stale_previews=True)
    job.updated_at = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=3600
    )

    db = MagicMock()
    # Job appears in both stuck_postprocess (#3) and jobs_with_previews (#4).
    db.query.side_effect = _query_chain_maker([[], [], [job], [job]])

    monkeypatch.setattr(jobs_router, "is_dev_mode", lambda: True)

    with patch.object(
        jobs_router, "apply_job_state_devmode"
    ) as devmode_reset, patch.object(
        jobs_router, "active_generate_previews_job_ids", return_value=set()
    ), patch.object(jobs_router, "StageState"):
        jobs_router._cleanup_stale_jobs(db)

    devmode_reset.assert_called_once()
