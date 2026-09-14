"""Stuck-preview auto-recovery in ``_cleanup_stale_jobs``.

2026-09-12 prod audit of 1.6.15: the sweep re-flagged ~25 jobs every cycle
and re-enqueued ``generate_previews`` for jobs whose raw output was long
gone, because its writes mutated ``job.disc_payload`` in place (a plain JSON
column) and never reached the database — so the attempt counter never
moved and the cap never fired.
"""

from __future__ import annotations

import datetime
import uuid
from unittest.mock import MagicMock, patch

import pytest

from api.routers import jobs as jobs_router
from core.preview_recovery import PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS


def _query_chain_maker(all_results: list[list]):
    idx = {"n": 0}

    def query(_model=None):
        i = idx["n"]
        idx["n"] += 1
        chain = MagicMock()
        chain.filter.return_value = chain
        chain.all.return_value = all_results[i] if i < len(all_results) else []
        return chain

    return query


def _stuck_preview_job(*, attempts: int | None, status: str = "running"):
    job = MagicMock()
    job.id = uuid.uuid4()
    job.celery_task_id = None
    job.rip_state = "completed"
    job.job_status = "completed"
    job.post_paths = {"t1": "Movies/a.mkv"}
    job.ripped_files = {}
    job.updated_at = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    previews = {
        "status": status,
        "tracks": {"t1": {"status": "queued"}},
        "updated_at": "2026-01-01T00:00:00",
    }
    if attempts is not None:
        previews["auto_recovery_attempts"] = attempts
    job.disc_payload = {"previews": previews}
    return job


@pytest.fixture
def sweep_env(monkeypatch):
    monkeypatch.setattr(jobs_router, "STALE_JOB_TIMEOUT_SECONDS", 900)
    monkeypatch.setattr(jobs_router, "_collect_celery_tasks_on_workers", lambda: (True, set()))
    monkeypatch.setattr(jobs_router, "active_generate_previews_job_ids", lambda: set())
    with patch("core.job_validation.validate_previews", return_value=(True, [])), patch.object(
        jobs_router, "generate_previews"
    ) as gen, patch.object(jobs_router, "flag_modified") as flagged, patch.object(
        jobs_router, "build_preview_regeneration_state"
    ) as regen:
        gen.delay.return_value = MagicMock(id="task-1")
        yield {"gen": gen, "flagged": flagged, "regen": regen}


def _run(job, sweep_env):
    db = MagicMock()
    db.query.side_effect = _query_chain_maker([[], [], [], [job]])
    jobs_router._cleanup_stale_jobs(db)
    return db


def test_recovery_write_is_flagged_and_counts_the_attempt(sweep_env):
    job = _stuck_preview_job(attempts=None)
    sweep_env["regen"].return_value = ({"t1": {"status": "queued"}}, ["t1"], "queued")

    db = _run(job, sweep_env)

    sweep_env["flagged"].assert_called_once_with(job, "disc_payload")
    db.commit.assert_called()
    sweep_env["gen"].delay.assert_called_once_with(str(job.id), ["t1"])
    previews = job.disc_payload["previews"]
    assert previews["auto_recovery_attempts"] == 1
    assert previews["status"] == "running"


def test_recovery_gives_up_at_the_cap_even_when_previews_validate(sweep_env):
    """Validation passing used to reset the counter to 0 — an infinite loop
    whenever the worker kept failing on a valid-looking job."""
    job = _stuck_preview_job(attempts=PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS)
    sweep_env["regen"].return_value = ({"t1": {"status": "queued"}}, ["t1"], "queued")

    _run(job, sweep_env)

    sweep_env["gen"].delay.assert_not_called()
    previews = job.disc_payload["previews"]
    assert previews["status"] == "failed"
    assert previews["auto_recovery_attempts"] == PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS
    assert "gave up" in previews["auto_recovery_last_error"]
    sweep_env["flagged"].assert_called_once_with(job, "disc_payload")


def test_settling_from_disk_is_not_an_attempt(sweep_env):
    """All manifests present: status is settled to completed, nothing is
    enqueued, and the counter does not move."""
    job = _stuck_preview_job(attempts=2, status="queued")
    sweep_env["regen"].return_value = ({"t1": {"status": "completed"}}, [], "completed")

    _run(job, sweep_env)

    sweep_env["gen"].delay.assert_not_called()
    previews = job.disc_payload["previews"]
    assert previews["status"] == "completed"
    assert previews["auto_recovery_attempts"] == 2
    sweep_env["flagged"].assert_called_once_with(job, "disc_payload")
