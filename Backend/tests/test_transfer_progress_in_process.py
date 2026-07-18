"""
Coverage for the in-process transfer-progress state update (#365 cleanup).

When ``_post_transfer_progress`` was rewritten from an HTTP POST to
``/jobs/{id}/transfer-progress`` into a direct DB write, the existing
endpoint test (which exercises the legacy HTTP path) became insufficient
to catch regressions in the new in-process code.

These tests mirror the pattern from ``test_postprocess_complete_in_process.py``
and ``test_transfer_complete_in_process.py``: contract assertions against
the worker helper with explicit regression guards against re-introducing
the HTTP coupling that caused #378.

See ``docs/ADR-001-postprocess-collapse.md``.
"""
import uuid

import pytest

from api import crud, models
from workers.tasks import (
    _post_transfer_progress,
    _TRANSFER_PROGRESS_LAST_ACCEPT,
    _TRANSFER_PROGRESS_RATE_LIMIT_SECONDS,
)


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    """Clear the in-process rate-limit dict between tests so a fresh
    test isn't throttled by a previous test's recent call."""
    _TRANSFER_PROGRESS_LAST_ACCEPT.clear()
    yield
    _TRANSFER_PROGRESS_LAST_ACCEPT.clear()


@pytest.fixture
def job_transferring(test_db):
    """A job with transfer_state=running — the only state in which the
    progress callback is allowed to write."""
    session = test_db()
    try:
        disc = models.Disc(
            id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}",
            disc_number=1,
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="completed",
            transfer_state="running",
            transfer_progress=0,
            phase="transfer",
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield str(job.id)
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Success path
# ──────────────────────────────────────────────────────────────────────────


def test_first_progress_call_writes_to_db(test_db, job_transferring):
    """The first call (no prior rate-limit entry for this job_id) lands."""
    _post_transfer_progress(job_transferring, transfer_progress=42)

    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        assert job.transfer_progress == 42
    finally:
        session.close()


def test_progress_clamped_to_valid_range(test_db, job_transferring):
    """Out-of-range progress values are clamped to [0, 100] —
    matches the HTTP endpoint's clamp logic."""
    _post_transfer_progress(job_transferring, transfer_progress=150)

    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        assert job.transfer_progress == 100
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Rate limit
# ──────────────────────────────────────────────────────────────────────────


def test_rate_limit_throttles_rapid_successive_calls(test_db, job_transferring):
    """Two calls within the rate-limit window: only the first lands.
    Critical for the hot path — transfer progress can fire hundreds of
    times per second; we don't want to spam the DB with that volume."""
    _post_transfer_progress(job_transferring, transfer_progress=10)
    # Second call immediately after — should be throttled.
    _post_transfer_progress(job_transferring, transfer_progress=99)

    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        # transfer_progress should still be 10 (the throttled second
        # call never landed).
        assert job.transfer_progress == 10
    finally:
        session.close()


def test_rate_limit_releases_after_window_elapses(
    test_db, job_transferring, monkeypatch
):
    """Calls more than the rate-limit window apart both land."""
    import workers.tasks as tasks_mod

    # Pin time so the test is deterministic.
    fake_time = [1000.0]

    def fake_now():
        return fake_time[0]

    monkeypatch.setattr(tasks_mod.time, "time", fake_now)

    _post_transfer_progress(job_transferring, transfer_progress=10)
    # Advance past the rate-limit window.
    fake_time[0] += _TRANSFER_PROGRESS_RATE_LIMIT_SECONDS + 0.1
    _post_transfer_progress(job_transferring, transfer_progress=80)

    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        assert job.transfer_progress == 80
    finally:
        session.close()


def test_rate_limit_is_per_job(test_db):
    """Rate limit on job A does not block job B — the throttle dict is
    keyed per-job_id."""
    session = test_db()
    try:
        # Create two jobs in transferring state.
        ids = []
        for _ in range(2):
            disc = models.Disc(
                id=str(uuid.uuid4()), content_hash=f"h-{uuid.uuid4().hex[:8]}",
            )
            session.add(disc)
            session.flush()
            job = models.Job(
                disc_id=disc.id, disc_num="1", mount_point="/m",
                rip_state="completed",
                transfer_state="running", transfer_progress=0,
            )
            session.add(job)
            session.commit()
            ids.append(str(job.id))
    finally:
        session.close()

    _post_transfer_progress(ids[0], transfer_progress=33)
    # Immediately after — different job, not throttled.
    _post_transfer_progress(ids[1], transfer_progress=66)

    session = test_db()
    try:
        a = crud.get_job(session, ids[0])
        b = crud.get_job(session, ids[1])
        assert a.transfer_progress == 33
        assert b.transfer_progress == 66
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# State guard
# ──────────────────────────────────────────────────────────────────────────


def test_drop_progress_when_transfer_not_running(test_db, job_transferring):
    """Progress callbacks against a job whose transfer is not currently
    running (e.g. already completed, or paused, or failed) are silently
    dropped — matches the HTTP endpoint's 409 behaviour. Prevents stale
    in-flight callbacks from re-zeroing a completed job's progress."""
    # Flip the job out of running.
    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        job.transfer_state = "completed"
        job.transfer_progress = 100
        session.commit()
    finally:
        session.close()

    _post_transfer_progress(job_transferring, transfer_progress=5)

    session = test_db()
    try:
        job = crud.get_job(session, job_transferring)
        # Not overwritten by the late callback.
        assert job.transfer_state == "completed"
        assert job.transfer_progress == 100
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_unknown_job_does_not_raise(test_db):
    """Callback for a job that doesn't exist (deleted between worker
    enqueue and progress tick) logs a warning and returns cleanly."""
    _post_transfer_progress(str(uuid.uuid4()), transfer_progress=50)
    # If we got here, no exception propagated — pass.


# ──────────────────────────────────────────────────────────────────────────
# Regression guard
# ──────────────────────────────────────────────────────────────────────────


def test_no_http_layer_dependency(monkeypatch):
    """Regression guard: the in-process callback must NEVER fall back
    to ``requests.post`` on any code path. Restoring the HTTP coupling
    would re-introduce the #378-class fragility (worker depending on a
    reachable API just to report progress) and hammer the API with
    one roundtrip per progress tick."""
    import workers.tasks as tasks_mod

    calls = []

    def fail_on_post(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "in-process transfer-progress must not POST anywhere — "
            f"saw call to requests.post with {args!r}"
        )

    monkeypatch.setattr(tasks_mod.requests, "post", fail_on_post)
    # Unknown job is fine — we just want to verify no HTTP call was
    # attempted at any point in the dispatch.
    _post_transfer_progress(str(uuid.uuid4()), transfer_progress=42)
    assert calls == [], (
        "in-process transfer-progress should never invoke requests.post; "
        f"saw {len(calls)} unexpected call(s)"
    )
