"""``one_at_a_time`` lock contention: only the rip fails its job.

2026-09-10 prod: the stale sweep re-enqueued ``generate_previews`` for a job
whose rip AND transfer were complete; the shared lock was busy, and the
decorator flipped that finished job to failed "Lock held; another job is
running". Side tasks must retry, never fail the job.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from filelock import Timeout

from workers import tasks


class _BusyLock:
    def __init__(self, *_a, **_k):
        pass

    def __enter__(self):
        raise Timeout("busy")

    def __exit__(self, *_a):
        return False


class _Retry(Exception):
    pass


@pytest.fixture
def busy_lock(monkeypatch):
    monkeypatch.setattr(tasks, "FileLock", _BusyLock)
    monkeypatch.setattr(tasks, "_cleanup_stale_lock", lambda _p: None)
    crud = MagicMock()
    monkeypatch.setattr(tasks, "crud", crud)
    apply_state = MagicMock()
    monkeypatch.setattr(tasks, "apply_job_state", apply_state)
    db = MagicMock()

    @contextmanager
    def _session():
        yield db

    monkeypatch.setattr(tasks, "db_session", _session)
    return {"crud": crud, "apply_state": apply_state, "db": db}


def _wrapped(name: str):
    def body(self, job_id):  # pragma: no cover - never reached under a busy lock
        raise AssertionError("body must not run when the lock is busy")

    body.__name__ = name
    return tasks.one_at_a_time(body)


def test_non_rip_task_retries_and_leaves_the_job_alone(busy_lock):
    task = MagicMock()
    task.retry.side_effect = _Retry

    with pytest.raises(_Retry):
        _wrapped("generate_previews")(task, "job-1")

    task.retry.assert_called_once()
    assert task.retry.call_args.kwargs["countdown"] == tasks.LOCK_CONTENTION_RETRY_SECONDS
    assert task.retry.call_args.kwargs["max_retries"] == tasks.LOCK_CONTENTION_MAX_RETRIES
    busy_lock["crud"].get_job.assert_not_called()
    busy_lock["apply_state"].assert_not_called()


def test_rip_task_still_fails_its_job_on_a_busy_lock(busy_lock):
    job = MagicMock()
    job.rip_state = "pending"
    busy_lock["crud"].get_job.return_value = job
    task = MagicMock()

    with pytest.raises(RuntimeError, match="already running"):
        _wrapped("rip_disc")(task, "job-2")

    task.retry.assert_not_called()
    busy_lock["apply_state"].assert_called_once()
    updates = busy_lock["apply_state"].call_args.kwargs["updates"]
    assert updates["job_status"] == "failed"
    assert updates["rip_state"] == "failed"
    assert updates["error_reason"] == "Lock held; another job is running"
