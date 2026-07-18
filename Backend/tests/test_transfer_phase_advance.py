"""
Coverage for the transfer_phase sub-phase indicator (#365 cleanup).

Phase 2 introduced ``Job.transfer_phase`` (``"preparing" | "transferring"
| "verifying"``) and ``start_transfer`` set it to ``"preparing"`` at the
top of the prep body. The follow-up cleanup adds the two remaining
transitions so the indicator actually progresses through all three
sub-phases:

  * ``preparing → transferring`` — when the in-API transfer endpoint
    begins moving files
  * ``transferring → verifying`` — when the first hash-progress
    callback fires (verification has started)

The transitions are funneled through ``_advance_transfer_phase`` in
``api/routers/jobs.py``. These tests pin the helper's contract so a
regression in the indicator (e.g., the helper losing its
exception-safety or its idempotency) fails loud.
"""
from types import SimpleNamespace
import uuid

import pytest

from api import models
from api.routers.jobs import _advance_transfer_phase
from core.job_state import apply_job_state


# ──────────────────────────────────────────────────────────────────────────
# Real-job behaviour
# ──────────────────────────────────────────────────────────────────────────


@pytest.fixture
def job_with_transfer_phase(test_db):
    """Job at the boundary the helper is called from — rip done, prep
    done, transfer_state=ready, transfer_phase=preparing."""
    session = test_db()
    try:
        disc = models.Disc(
            id=str(uuid.uuid4()),
            content_hash=f"h-{uuid.uuid4().hex[:8]}",
        )
        session.add(disc)
        session.flush()
        job = models.Job(
            id=str(uuid.uuid4()),
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/sr0",
            rip_state="completed",
            transfer_state="ready",
            transfer_phase="preparing",
            phase="transfer",
            stage_profile="hit",
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        yield session, job
    finally:
        session.close()


def test_advances_preparing_to_transferring(job_with_transfer_phase):
    """The first transition in the sub-phase sequence."""
    session, job = job_with_transfer_phase
    assert job.transfer_phase == "preparing"

    _advance_transfer_phase(session, job, "transferring",
                            reason="test: enter transferring")
    session.refresh(job)
    assert job.transfer_phase == "transferring"


def test_advances_transferring_to_verifying(job_with_transfer_phase):
    """The second transition. Sequential calls walk the indicator
    through the chain without surprises."""
    session, job = job_with_transfer_phase
    _advance_transfer_phase(session, job, "transferring", reason="test: a")
    _advance_transfer_phase(session, job, "verifying", reason="test: b")
    session.refresh(job)
    assert job.transfer_phase == "verifying"


# ──────────────────────────────────────────────────────────────────────────
# Idempotency
# ──────────────────────────────────────────────────────────────────────────


def test_no_op_when_already_at_target_phase(job_with_transfer_phase, monkeypatch):
    """The hash_progress_callback fires many times during a long
    verification. The helper must not produce a DB write per call —
    only the first call should hit ``apply_job_state``. Otherwise the
    progress callbacks would generate hundreds of redundant transactions
    on a multi-GB transfer."""
    session, job = job_with_transfer_phase
    job.transfer_phase = "verifying"
    session.commit()

    call_count = {"n": 0}
    real_apply = apply_job_state

    def tracking_apply(*args, **kwargs):
        call_count["n"] += 1
        return real_apply(*args, **kwargs)

    monkeypatch.setattr("api.routers.jobs.apply_job_state", tracking_apply)

    # Call the helper repeatedly with the current phase.
    for _ in range(5):
        _advance_transfer_phase(session, job, "verifying", reason="test: noop")

    assert call_count["n"] == 0, (
        "helper must short-circuit when transfer_phase is already at "
        "the target; otherwise it spams the DB on every progress callback"
    )


def test_only_first_call_writes_when_transitioning(job_with_transfer_phase, monkeypatch):
    """Sequence of calls with the same target — only the first call
    that observes a different current phase actually writes."""
    session, job = job_with_transfer_phase

    call_count = {"n": 0}
    real_apply = apply_job_state

    def tracking_apply(*args, **kwargs):
        call_count["n"] += 1
        return real_apply(*args, **kwargs)

    monkeypatch.setattr("api.routers.jobs.apply_job_state", tracking_apply)

    # First call — actually transitions.
    _advance_transfer_phase(session, job, "transferring", reason="test")
    # Subsequent calls — already at target, no writes.
    for _ in range(5):
        _advance_transfer_phase(session, job, "transferring", reason="test")

    assert call_count["n"] == 1


# ──────────────────────────────────────────────────────────────────────────
# Exception safety
# ──────────────────────────────────────────────────────────────────────────


def test_swallows_exception_when_apply_fails(job_with_transfer_phase, monkeypatch):
    """A state-write failure during an in-flight transfer must NOT
    propagate — the file movement is already happening and we don't
    want to crash mid-transfer because the indicator failed to update.
    The helper logs and returns cleanly."""
    session, job = job_with_transfer_phase

    def boom(*args, **kwargs):
        raise RuntimeError("simulated DB hiccup")

    monkeypatch.setattr("api.routers.jobs.apply_job_state", boom)

    # Should NOT raise.
    _advance_transfer_phase(session, job, "transferring", reason="test")


def test_handles_simplenamespace_job_without_id(monkeypatch):
    """Defensive: the helper logs with ``getattr(job, "id", "?")``. If
    a caller hands it a job-shaped object without an ``id`` attribute
    (e.g. a unit-test fake), the logging path must not raise."""
    fake_job = SimpleNamespace(transfer_phase="preparing")

    def boom(*args, **kwargs):
        raise RuntimeError("simulated")

    monkeypatch.setattr("api.routers.jobs.apply_job_state", boom)
    # No ``id`` attr, but the helper should still log + swallow.
    _advance_transfer_phase(None, fake_job, "transferring", reason="test")


# ──────────────────────────────────────────────────────────────────────────
# WebSocket / context noise — sub-phase advances must NOT emit
# context_changed
# ──────────────────────────────────────────────────────────────────────────


def test_advance_does_not_emit_context_changed(job_with_transfer_phase, monkeypatch):
    """Sub-phase updates fire from hot paths (progress callbacks) and
    must use ``skip_context_changed=True`` so the per-job WebSocket
    isn't flooded with context-rebuild notifications. The job-progress
    WebSocket already broadcasts ``transfer_phase`` via the regular
    progress payload."""
    session, job = job_with_transfer_phase

    captures = []

    def fake_emit(job_, normalized_updates, *, skip_context_changed=False):
        captures.append({"updates": dict(normalized_updates),
                         "skip_context_changed": skip_context_changed})

    monkeypatch.setattr(
        "core.job_state._emit_job_state_websocket_updates",
        fake_emit,
    )

    _advance_transfer_phase(session, job, "transferring", reason="test")

    assert len(captures) == 1, captures
    assert captures[0]["skip_context_changed"] is True, (
        "sub-phase advances must skip the per-job context_changed "
        "broadcast to avoid flooding the UI from progress callbacks"
    )
    assert captures[0]["updates"].get("transfer_phase") == "transferring"
