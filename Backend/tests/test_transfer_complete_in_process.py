"""
Coverage for the in-process transfer-complete state update (#365 cleanup).

When ``_post_transfer_complete_callback`` was rewritten from an HTTP POST
to ``/jobs/{id}/transfer-complete`` into a direct DB write that calls the
in-API ``_complete_transfer`` / ``_fail_transfer`` helpers, the existing
``test_transfer_complete_endpoint.py`` (and integration coverage) only
exercises the legacy HTTP endpoint (which stays registered for one
release as an in-flight task safety net). The **new production code
path** — the in-process invocation — had zero direct coverage.

These tests mirror the test_postprocess_complete_in_process.py pattern:
contract assertions against the worker helper instead of the HTTP
endpoint, with explicit regression guards against the failure modes
that bit us before (#378 fragility from worker → API HTTP coupling).

See ``docs/ADR-001-postprocess-collapse.md``.
"""
import uuid

import pytest

from api import crud, models
from workers.tasks import _post_transfer_complete_callback


@pytest.fixture
def job_running_transfer(test_db):
    """A job in the state the transfer worker hands off from at
    end-of-transfer: rip + post complete, transfer running."""
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
            transfer_phase="verifying",
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


def test_in_process_callback_applies_transfer_complete_state(
    test_db, job_running_transfer
):
    """Success path drives the job to transfer_state=completed via the
    in-API _complete_transfer helper. For hit profile, the job also
    advances to job_status=completed and phase=complete."""
    job_id = job_running_transfer
    dest_paths = ["/library/Movies/Test/Test.1080p.mkv"]

    _post_transfer_complete_callback(
        job_id, success=True, dest_paths=dest_paths,
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.transfer_state == "completed"
        # hit profile fixture → job ends complete
        assert job.job_status == "completed"
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Failure path
# ──────────────────────────────────────────────────────────────────────────


def test_in_process_callback_applies_transfer_failed_state(
    test_db, job_running_transfer
):
    """Failure path drives the job to transfer_state=failed via
    _fail_transfer. error_reason persisted."""
    job_id = job_running_transfer

    _post_transfer_complete_callback(
        job_id, success=False, error_reason="rsync timeout to remote host",
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.transfer_state == "failed"
        # error_reason or transfer_error should reflect the failure.
        err = (job.error_reason or "") + (job.transfer_error or "")
        assert "rsync timeout" in err
    finally:
        session.close()


def test_in_process_callback_failure_defaults_error_reason(
    test_db, job_running_transfer
):
    """If the worker doesn't pass an error_reason, the helper supplies a
    default rather than crashing or persisting an empty string."""
    job_id = job_running_transfer

    _post_transfer_complete_callback(job_id, success=False)

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.transfer_state == "failed"
        err = (job.error_reason or "") + (job.transfer_error or "")
        assert err  # non-empty
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Idempotency + edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_in_process_callback_idempotent_when_already_completed(
    test_db, job_running_transfer
):
    """Second call when transfer_state is already completed is a no-op.
    Matches the HTTP endpoint's early-return guard and protects against
    retry-after-timeout races where the first call already won."""
    job_id = job_running_transfer
    # First call → completed.
    _post_transfer_complete_callback(
        job_id, success=True, dest_paths=["/library/M/a.mkv"],
    )

    # Second call attempts to mark failed — should be ignored.
    _post_transfer_complete_callback(
        job_id, success=False, error_reason="should be ignored",
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.transfer_state == "completed"  # not flipped to failed
        err = (job.error_reason or "") + (job.transfer_error or "")
        assert "should be ignored" not in err
    finally:
        session.close()


def test_in_process_callback_unknown_job_does_not_raise(test_db):
    """A callback for a job that doesn't exist (deleted between worker
    enqueue and transfer completion) logs a warning and returns
    cleanly — never propagates an exception that would crash the worker."""
    # Should not raise.
    _post_transfer_complete_callback(
        str(uuid.uuid4()), success=True, dest_paths=["/library/x.mkv"],
    )


def test_in_process_callback_no_http_layer_dependency(monkeypatch):
    """Regression guard: the in-process callback must NOT fall back to
    ``requests.post``. If a future refactor accidentally restores the
    HTTP fallback path (same fragility #378 inflicted on us), this test
    fails — preventing a silent regression where the worker suddenly
    needs the API to be reachable to complete transfer."""
    import workers.tasks as tasks_mod

    calls = []

    def fail_on_post(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "in-process transfer-complete callback must not POST anywhere — "
            f"saw call to requests.post with {args!r}"
        )

    monkeypatch.setattr(tasks_mod.requests, "post", fail_on_post)
    # Unknown job is fine — we just want to verify no HTTP call was
    # attempted along the way.
    _post_transfer_complete_callback(
        str(uuid.uuid4()), success=True, dest_paths=["/library/x.mkv"],
    )
    assert calls == [], (
        "in-process transfer-complete callback should never invoke "
        f"requests.post; saw {len(calls)} unexpected call(s)"
    )
