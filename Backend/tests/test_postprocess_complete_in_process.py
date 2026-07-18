"""
Coverage for the in-process postprocess-complete state update (#365 cleanup).

When PR #427 commit 3 landed, the worker's
``_post_postprocess_complete_callback`` was rewritten from an HTTP POST
to ``/jobs/{id}/postprocess-complete`` into a direct DB write that
calls ``StageState`` in the worker's own process. The existing
``test_postprocess_complete_endpoint.py`` tests still cover the legacy
HTTP endpoint (which stays registered for one release as an in-flight
task safety net), but the **new production code path** — the in-process
DB write — had zero direct coverage.

These tests mirror the endpoint test suite's contract assertions
(success / failure / idempotency / unknown job) against the in-process
helper instead of the HTTP endpoint, so a regression in the new path
fails loud.

See ``docs/ADR-001-postprocess-collapse.md`` for the architectural
context.
"""
import uuid

import pytest

from api import crud, models
from workers.tasks import _post_postprocess_complete_callback


@pytest.fixture
def job_running_postprocess(test_db):
    """A job in the state the worker hands off from at end-of-prep:
    rip completed, postprocess running (signalled by
    transfer_phase="preparing" — the post-3d collapsed-model invariant),
    phase=postprocess, transfer pending. Mirrors the equivalent fixture
    in ``test_postprocess_complete_endpoint.py`` for parity. The
    post_state column is still set for backward-compat with any callers
    that read it directly; the column is being dropped over #365 5d
    follow-ups."""
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
            phase="postprocess",
            transfer_phase="preparing",  # the new "running" signal
            transfer_state="pending",
            stage_profile="hit",
            label_state="skipped",  # hit profile
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


def test_in_process_callback_applies_postprocess_complete_state(
    test_db, job_running_postprocess
):
    """Success path mirrors what the HTTP handler used to do:
    post_state=completed, phase=transfer, transfer_state=ready,
    post_paths persisted."""
    job_id = job_running_postprocess
    post_paths = {"title-1": "Movies/My Film (2024)/My Film.1080p.mkv"}

    _post_postprocess_complete_callback(
        job_id,
        success=True,
        post_paths=post_paths,
        post_progress=100,
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.derived_post_state == "completed"
        assert job.phase == "transfer"
        assert job.transfer_state == "ready"
        assert job.post_paths == post_paths
        assert job.post_progress == 100
    finally:
        session.close()


def test_in_process_callback_persists_disc_payload_updates(
    test_db, job_running_postprocess
):
    """The disc_payload_updates kwarg merges into the job's existing
    disc_payload — matching the HTTP handler's StageState.postprocess_complete
    call shape."""
    job_id = job_running_postprocess
    # Seed an initial disc_payload so we can verify the merge (not replace).
    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        job.disc_payload = {"existing_key": "kept"}
        session.commit()
    finally:
        session.close()

    _post_postprocess_complete_callback(
        job_id,
        success=True,
        post_paths={"t1": "out.mkv"},
        disc_payload_updates={"final_hashes": {"t1": "deadbeef"}},
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        # Existing key preserved.
        assert job.disc_payload.get("existing_key") == "kept"
        # New key merged in.
        assert job.disc_payload.get("final_hashes") == {"t1": "deadbeef"}
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Failure path
# ──────────────────────────────────────────────────────────────────────────


def test_in_process_callback_applies_postprocess_failed_state(
    test_db, job_running_postprocess
):
    """Failure path: post_state=failed, job_status=failed, error_reason
    persisted. Matches StageState.postprocess_failed semantics."""
    job_id = job_running_postprocess

    _post_postprocess_complete_callback(
        job_id,
        success=False,
        error_reason="rename failed: disk full",
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.derived_post_state == "failed"
        assert job.job_status == "failed"
        assert "disk full" in (job.error_reason or "")
    finally:
        session.close()


def test_in_process_callback_failure_defaults_error_reason(
    test_db, job_running_postprocess
):
    """If the worker doesn't pass an error_reason, the default still
    surfaces *something* (matches the HTTP endpoint's body validator
    contract — error_reason was required on the HTTP side; here the
    helper supplies a default rather than crashing)."""
    job_id = job_running_postprocess

    _post_postprocess_complete_callback(job_id, success=False)

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.derived_post_state == "failed"
        assert job.error_reason  # non-empty
    finally:
        session.close()


# ──────────────────────────────────────────────────────────────────────────
# Idempotency + edge cases
# ──────────────────────────────────────────────────────────────────────────


def test_in_process_callback_idempotent_when_already_completed(
    test_db, job_running_postprocess
):
    """Second call when post_state is already terminal is a no-op —
    matches the HTTP endpoint's early-return-on-terminal-state guard,
    and protects against the race where a retry-after-timeout fires
    after the first call already won."""
    job_id = job_running_postprocess
    # First call → completed.
    _post_postprocess_complete_callback(
        job_id, success=True,
        post_paths={"t1": "first.mkv"},
    )
    session = test_db()
    try:
        first_state = crud.get_job(session, job_id).post_paths
    finally:
        session.close()

    # Second call attempts to mark failed — should be ignored.
    _post_postprocess_complete_callback(
        job_id, success=False, error_reason="should be ignored",
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.derived_post_state == "completed"  # not flipped to failed
        assert job.post_paths == first_state  # not overwritten
        # Defensive: error_reason should NOT have been set by the
        # second (ignored) call.
        assert "should be ignored" not in (job.error_reason or "")
    finally:
        session.close()


def test_in_process_callback_idempotent_when_already_failed(
    test_db, job_running_postprocess
):
    """Symmetric idempotency: a job already in failed state is not
    re-promoted to completed."""
    job_id = job_running_postprocess
    _post_postprocess_complete_callback(
        job_id, success=False, error_reason="first failure",
    )

    _post_postprocess_complete_callback(
        job_id, success=True, post_paths={"t1": "would.overwrite.mkv"},
    )

    session = test_db()
    try:
        job = crud.get_job(session, job_id)
        assert job.derived_post_state == "failed"  # not flipped to completed
        # post_paths must not have been overwritten by the ignored call.
        assert job.post_paths != {"t1": "would.overwrite.mkv"}
    finally:
        session.close()


def test_in_process_callback_unknown_job_does_not_raise(test_db):
    """A callback for a job that doesn't exist (because it was deleted
    between worker enqueue and prep completion) logs a warning and
    returns cleanly — never propagates an exception that would crash
    the worker mid-postprocess."""
    # Should not raise.
    _post_postprocess_complete_callback(
        str(uuid.uuid4()), success=True, post_paths={"t1": "x.mkv"},
    )


def test_in_process_callback_no_http_layer_dependency(monkeypatch):
    """Regression guard: the in-process callback must NOT fall back to
    ``requests.post`` (the legacy HTTP path) under any circumstance.
    If a future refactor accidentally restores the HTTP dependency,
    this test fails — preventing a silent regression where the worker
    suddenly needs the API to be up to complete postprocess."""
    import workers.tasks as tasks_mod

    calls = []

    def fail_on_post(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError(
            "in-process callback must not POST anywhere — "
            f"saw call to requests.post with {args!r}"
        )

    monkeypatch.setattr(tasks_mod.requests, "post", fail_on_post)
    # Unknown job is fine — we just want to verify no HTTP call was
    # attempted along the way.
    _post_postprocess_complete_callback(
        str(uuid.uuid4()), success=True, post_paths={"t1": "x.mkv"},
    )
    assert calls == [], (
        "in-process callback should never invoke requests.post; "
        f"saw {len(calls)} unexpected call(s)"
    )
