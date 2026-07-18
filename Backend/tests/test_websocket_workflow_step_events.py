"""
Coverage for the WebSocket emission rendezvous on pipeline state transitions.

Every successful ``apply_job_state`` call invokes
``_emit_job_state_websocket_updates(job, normalized_updates, skip_context_changed=...)``,
which is the **single rendezvous point** between the state machine and the
WebSocket layer. From there it schedules ``_emit_to_job_workflow`` (a
``context_changed`` message on the unified channel) and any milestone
notifications. Worker-driven callbacks (rip-complete, postprocess-complete,
transfer-complete) never set ``skip_context_changed``, so they always trigger
the workflow emission.

Phase 2 of the postprocess collapse will collapse `postprocess` out of the
state machine and fold its emissions into the transfer phase. These tests
capture the **current** rendezvous contract — same input dict drives both
the DB write and the WS notification — so the refactor can diff against a
known baseline.

Phase 0 backfill — see ``docs/plans/postprocess-collapse-325-365.md``.
"""
import uuid
from unittest.mock import patch

import pytest

from api import models
from core.job_state import StageState, apply_job_state


def _seed_job(session, *, rip_state="running", post_state="pending", phase="rip"):
    """Minimal disc + job in the pre-rip-complete state.

    ``post_state`` is no longer a column (#365 step 5) — it's derived via
    ``Job.derived_post_state``. This helper translates the legacy intent
    into the underlying fields that drive the derivation, so the existing
    tests can keep their human-readable post_state vocabulary:

      - ``"pending"`` → no extra writes (rip_state pre-completion → None).
      - ``"running"`` → ``transfer_phase="preparing"`` (decision-table 3).
      - ``"ready"``   → ``label_state="skipped"`` (decision-table 6, hit
        profile path where postprocess can start).
      - ``"completed"`` → ``transfer_state="ready"`` (decision-table 5).
    """
    disc_id = str(uuid.uuid4())
    session.add(models.Disc(id=disc_id, content_hash=f"h-{uuid.uuid4().hex[:8]}"))
    job_id = str(uuid.uuid4())
    job_kwargs = dict(
        id=job_id, disc_id=disc_id,
        disc_num="1", mount_point="/mnt/sr0",
        rip_state=rip_state, phase=phase,
    )
    if post_state == "running":
        job_kwargs["transfer_phase"] = "preparing"
    elif post_state == "ready":
        job_kwargs["label_state"] = "skipped"
    elif post_state == "completed":
        job_kwargs["transfer_state"] = "ready"
    session.add(models.Job(**job_kwargs))
    session.commit()
    return job_id, disc_id


@pytest.fixture
def captured_ws():
    """Replace the WS rendezvous helper with a capture. Returns the list of
    (normalized_updates, skip_context_changed) tuples seen by the WS layer.
    """
    calls = []

    def capture(job, normalized_updates, *, skip_context_changed=False):
        calls.append({
            "job_id": str(getattr(job, "id", "")),
            "updates": dict(normalized_updates),
            "skip_context_changed": skip_context_changed,
        })

    with patch("core.job_state._emit_job_state_websocket_updates", side_effect=capture):
        yield calls


# ──────────────────────────────────────────────────────────────────────────
# Per-stage emission contract
# ──────────────────────────────────────────────────────────────────────────


def test_apply_job_state_emits_for_rip_state_transition(test_db, captured_ws):
    """A ``rip_state`` transition reaches the WS layer with that field in the
    normalized_updates payload."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session)
        job = session.query(models.Job).filter_by(id=job_id).first()
        apply_job_state(
            session, job,
            updates={"rip_state": "completed", "phase": "postprocess"},
            reason="test",
        )
    finally:
        session.close()

    rip_emissions = [c for c in captured_ws if c["updates"].get("rip_state") == "completed"]
    assert len(rip_emissions) == 1, captured_ws
    assert rip_emissions[0]["job_id"] == job_id
    assert rip_emissions[0]["skip_context_changed"] is False


def test_postprocess_complete_emits_post_state_and_phase_in_updates(test_db, captured_ws):
    """``StageState.postprocess_complete`` reaches the WS layer with the
    transfer-readiness payload — ``phase=transfer``, ``transfer_state=ready``,
    ``transfer_phase=None`` (clearing the "preparing" signal), and
    ``post_paths`` in the same dict. This is what the workflow UI keys off
    for the 'Postprocessing → ready to transfer' transition.

    #365 step 5 — the explicit ``post_state="completed"`` write was dropped;
    ``Job.derived_post_state`` returns ``"completed"`` from
    ``transfer_state="ready"`` via decision-table step 5. The rendezvous
    contract (single emission, same dict driving DB write + WS event) holds."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, rip_state="completed", post_state="running",
                              phase="postprocess")
        job = session.query(models.Job).filter_by(id=job_id).first()
        StageState.postprocess_complete(
            session, job,
            post_paths={"t1": "Movies/X/X.mkv"},
            reason="test",
        )
    finally:
        session.close()

    matching = [
        c for c in captured_ws
        if c["updates"].get("transfer_state") == "ready"
        and c["updates"].get("phase") == "transfer"
    ]
    assert len(matching) == 1, captured_ws
    assert matching[0]["job_id"] == job_id
    # post_paths must travel with the same payload — UI uses it to render
    # the per-title path summary on the transfer step.
    assert matching[0]["updates"].get("post_paths") == {"t1": "Movies/X/X.mkv"}


def test_postprocess_failed_emits_post_state_failed(test_db, captured_ws):
    """The failure path reaches the WS layer with the failure marker — UI
    can switch to an error display in a single round-trip.

    #365 step 5 — explicit ``post_state="failed"`` write was dropped;
    ``Job.derived_post_state`` returns ``"failed"`` from ``job_status=failed``
    via decision-table step 2."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, rip_state="completed", post_state="running",
                              phase="postprocess")
        job = session.query(models.Job).filter_by(id=job_id).first()
        StageState.postprocess_failed(
            session, job,
            error_reason="disk full",
            reason="test",
        )
    finally:
        session.close()

    failure = [c for c in captured_ws if c["updates"].get("job_status") == "failed"]
    assert len(failure) == 1, captured_ws
    assert failure[0]["updates"].get("error_reason") == "disk full"


# ──────────────────────────────────────────────────────────────────────────
# skip_context_changed contract
# ──────────────────────────────────────────────────────────────────────────


def test_apply_job_state_emits_with_skip_context_changed_when_caller_requests(
    test_db, captured_ws
):
    """User-driven POSTs (complete_label, start_postprocess, start_transfer,
    save_label) pass ``skip_context_changed=True`` so the frontend uses the
    POST response directly and doesn't trigger a redundant fetch overlay.
    The WS layer still sees the call (for milestone notifications, master
    coordinator updates) — just doesn't emit the per-workflow
    context_changed."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, rip_state="completed", post_state="ready",
                              phase="postprocess")
        job = session.query(models.Job).filter_by(id=job_id).first()
        # #365 step 5 — POST /postprocess/start no longer writes post_state;
        # transfer_phase="preparing" is the signal that postprocess is
        # running. Caller still passes skip_context_changed=True.
        apply_job_state(
            session, job,
            updates={"transfer_phase": "preparing"},
            reason="test user start_postprocess",
            skip_context_changed=True,
        )
    finally:
        session.close()

    assert len(captured_ws) == 1
    assert captured_ws[0]["skip_context_changed"] is True


def test_worker_callback_emissions_never_skip_context_changed(test_db, captured_ws):
    """The state-machine helpers used by worker callbacks
    (StageState.postprocess_complete, .postprocess_failed, etc.) call
    ``apply_job_state`` without skip_context_changed. The frontend depends
    on the resulting WS event to advance the workflow step indicator."""
    session = test_db()
    try:
        job_id, _ = _seed_job(session, rip_state="completed", post_state="running",
                              phase="postprocess")
        job = session.query(models.Job).filter_by(id=job_id).first()
        StageState.postprocess_complete(
            session, job,
            post_paths={"t1": "Movies/X/X.mkv"},
            reason="test",
        )
    finally:
        session.close()

    assert len(captured_ws) == 1
    assert captured_ws[0]["skip_context_changed"] is False
