"""
Coverage for the in-process rip-complete callback (#365 cleanup).

The rip-complete callback marks the MakeMKV copy boundary — the worker
signals "copy finished" and the API acks by setting
``rip_phase=verification`` and enqueueing ``rip_verification``.

Same conversion shape as ``test_rip_verification_complete_in_process.py``:
the worker now invokes the API handler directly via Python import;
``RipCallbackTransportError`` raise semantics preserved.

See ``docs/ADR-001-postprocess-collapse.md``.
"""
import uuid

import pytest

from api import crud, models
from workers.tasks import (
    _post_rip_complete_callback,
    RipCallbackTransportError,
)


@pytest.fixture
def job_running_rip_copy(test_db):
    """Job at the boundary the callback fires from: rip_state=running,
    rip_phase=None (copy in progress; verification hasn't started)."""
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
            rip_state="running",
            rip_phase=None,
            rip_progress=100,
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


def test_success_advances_to_verification_phase(
    test_db, job_running_rip_copy, monkeypatch
):
    """Success transitions rip_phase from None to 'verification' and
    the handler enqueues the rip_verification Celery task. Mock the
    enqueue so the test doesn't try to reach Redis."""
    enqueued = []
    monkeypatch.setattr(
        "api.routers.jobs.enqueue_rip_verification_for_job",
        lambda job_id, reason=None: enqueued.append({"job_id": job_id, "reason": reason}),
    )

    _post_rip_complete_callback(
        job_running_rip_copy, success=True,
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_running_rip_copy)
        # Handler sets rip_phase=verification at the copy boundary.
        assert job.rip_phase == "verification"
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Failure-raise semantics (preserves legacy retry contract)
# ──────────────────────────────────────────────────────────────────────────


def test_handler_exception_propagates_as_RipCallbackTransportError(
    test_db, job_running_rip_copy, monkeypatch
):
    """If the in-process handler raises, the callback wraps as
    RipCallbackTransportError so the worker's retry/escalation
    behaviour matches the pre-cleanup HTTP-transport-failure path."""
    def boom(**kwargs):
        raise RuntimeError("simulated handler explosion")

    monkeypatch.setattr(
        "api.routers.jobs.rip_complete_callback", boom,
    )

    with pytest.raises(RipCallbackTransportError) as excinfo:
        _post_rip_complete_callback(job_running_rip_copy, success=True)
    assert "simulated handler explosion" in str(excinfo.value)


def test_invalid_body_construction_raises_RipCallbackTransportError(
    test_db, job_running_rip_copy, monkeypatch
):
    """Programmer-error: bad arguments to RipCompleteRequest also raise
    as RipCallbackTransportError so the caller's failure handling
    treats it uniformly."""
    def boom(**kwargs):
        raise ValueError("simulated schema failure")

    monkeypatch.setattr(
        "api.routers.jobs.RipCompleteRequest", boom,
    )

    with pytest.raises(RipCallbackTransportError) as excinfo:
        _post_rip_complete_callback(
            job_running_rip_copy, success=False, error_reason="x",
        )
    assert "body construction failed" in str(excinfo.value)


def test_failure_with_no_error_reason_defaults(
    test_db, job_running_rip_copy, monkeypatch
):
    """Schema requires error_reason on failure. The worker callback
    supplies a default if the caller forgot. Failure must not crash
    on a missing reason."""
    captured = {}

    def capture_handler(job_id, body, db, client_host):
        captured["error_reason"] = body.error_reason

    monkeypatch.setattr(
        "api.routers.jobs.rip_complete_callback", capture_handler,
    )

    _post_rip_complete_callback(job_running_rip_copy, success=False)
    assert captured.get("error_reason"), (
        "worker must supply a non-empty default error_reason when caller omits"
    )


# ──────────────────────────────────────────────────────────────────────────
# Regression guard
# ──────────────────────────────────────────────────────────────────────────


def test_no_http_layer_dependency(test_db, job_running_rip_copy, monkeypatch):
    """Regression guard: even on the rip-complete callback (the
    copy-boundary marker), no code path may fall back to ``requests.post``.
    Mirrors the regression guards in test_postprocess_complete_in_process.py
    et al."""
    import workers.tasks as tasks_mod

    # Mock the rip_verification enqueue so the handler's success path
    # doesn't try to reach Redis.
    monkeypatch.setattr(
        "api.routers.jobs.enqueue_rip_verification_for_job",
        lambda job_id, reason=None: None,
    )

    calls = []

    def fail_on_post(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "in-process rip-complete must not POST anywhere — "
            f"saw call to requests.post with {args!r}"
        )

    monkeypatch.setattr(tasks_mod.requests, "post", fail_on_post)
    _post_rip_complete_callback(job_running_rip_copy, success=True)
    assert calls == [], (
        "in-process rip-complete should never invoke requests.post; "
        f"saw {len(calls)} unexpected call(s)"
    )
