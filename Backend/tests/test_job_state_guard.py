"""
Tests for ``validate_job_state_transition`` — the synchronous guard that
``apply_job_state`` runs before persisting any state change to a job row.

The guard enforces:
- job_status transitions (forward-only, with running ↔ validating allowed)
- stage_state transitions (rip_state, post_state, transfer_state, label_state,
  finalize_state) — terminal states are sticky except for transfer retry
- phase ordering — phase can only advance if the prior stage states are
  complete; label is miss-profile-only

Phase 0 tests for #325 + #365: the postprocess collapse will change the
phase rules (no more 'postprocess' phase; transfer absorbs its prereqs)
and the stage_state rules (post_state may become derived). Capturing the
current contract first makes the refactor diff legible.

See ``docs/plans/postprocess-collapse-325-365.md``.
"""
from types import SimpleNamespace

import pytest

from core.job_state import StateViolation, validate_job_state_transition


# ──────────────────────────────────────────────────────────────────────────
# job_status guards (pre-existing)
# ──────────────────────────────────────────────────────────────────────────


def test_job_status_allows_running_validating_toggle():
    job = SimpleNamespace(job_status="running")
    validate_job_state_transition(job, {"job_status": "validating"})
    validate_job_state_transition(SimpleNamespace(job_status="validating"), {"job_status": "running"})


def test_job_status_disallows_completed_to_running():
    job = SimpleNamespace(job_status="completed")
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"job_status": "running"})


def test_failed_to_running_needs_the_recovery_flag_and_it_ships():
    """Recovery is a FIRST-CLASS transition, not a devmode privilege: the
    old devmode-only escape hatch was stripped from release builds, so
    "Retry processing" on a failed job 409'd on every prod install (caught
    live on 1.6.10)."""
    job = SimpleNamespace(job_status="failed")
    # Without the flag: still refused (nothing else may leave failed).
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"job_status": "running"})
    # With it: the resume endpoints may go to pending/running — nothing else.
    validate_job_state_transition(job, {"job_status": "running"}, allow_recovery=True)
    validate_job_state_transition(job, {"job_status": "pending"}, allow_recovery=True)
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"job_status": "completed"}, allow_recovery=True)
    # completed stays terminal even for recovery.
    with pytest.raises(StateViolation):
        validate_job_state_transition(
            SimpleNamespace(job_status="completed"), {"job_status": "running"}, allow_recovery=True
        )


# ──────────────────────────────────────────────────────────────────────────
# Stage-state guards (pre-existing + extensions)
# ──────────────────────────────────────────────────────────────────────────


def test_stage_state_disallows_backward_transition():
    job = SimpleNamespace(post_state="completed")
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"post_state": "running"})


def test_stage_state_allows_pending_ready_reclassification():
    job = SimpleNamespace(transfer_state="pending")
    validate_job_state_transition(job, {"transfer_state": "ready"})
    validate_job_state_transition(SimpleNamespace(transfer_state="ready"), {"transfer_state": "pending"})


def test_stage_state_disallows_running_to_pending_for_non_transfer_fields():
    """A stage running → pending is normally a forbidden backward move."""
    job = SimpleNamespace(post_state="running")
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"post_state": "pending"})


def test_transfer_state_running_to_pending_is_allowed_for_retry():
    """Transfer is the **one** stage where running → pending is permitted —
    used when the start fails (share down, etc.) so the frontend can
    re-attempt without the user having to escalate to a full retry."""
    job = SimpleNamespace(transfer_state="running")
    validate_job_state_transition(job, {"transfer_state": "pending"})  # no raise


def test_transfer_state_failed_to_running_is_allowed_for_retry():
    """Failed → running is the Retry Transfer path. The same exception does
    NOT apply to rip_state, post_state, or label_state. Transfer transitions
    also pull in stage-dependency validation (rip + post must be complete),
    so the test job is positioned post-postprocess."""
    job = SimpleNamespace(
        stage_profile="hit",
        rip_state="completed", label_state="skipped",
        post_state="completed", transfer_state="failed",
        phase="transfer",
    )
    validate_job_state_transition(job, {"transfer_state": "running"})  # no raise

    # Other stage fields keep the strict terminal-sticky rule.
    with pytest.raises(StateViolation):
        validate_job_state_transition(
            SimpleNamespace(post_state="failed"), {"post_state": "running"},
        )


def test_stage_state_rejects_unknown_value():
    job = SimpleNamespace(rip_state="running")
    with pytest.raises(StateViolation):
        validate_job_state_transition(job, {"rip_state": "bogus_value"})


# ──────────────────────────────────────────────────────────────────────────
# Phase-transition guards (key for #325 + #365)
# ──────────────────────────────────────────────────────────────────────────


def _miss_job(**overrides):
    """A miss-profile job at the rip stage. Overrides let each test position
    the job at the boundary it cares about."""
    defaults = dict(
        stage_profile="miss",
        rip_state="running",
        label_state=None,
        post_state="pending",
        transfer_state="pending",
        phase="rip",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _hit_job(**overrides):
    defaults = dict(
        stage_profile="hit",
        rip_state="running",
        label_state="skipped",
        post_state="pending",
        transfer_state="pending",
        phase="rip",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def test_phase_label_rejected_on_hit_profile():
    """The label phase is miss-only. A hit job cannot transition to label."""
    job = _hit_job(rip_state="completed")
    with pytest.raises(StateViolation, match="label phase only for miss"):
        validate_job_state_transition(job, {"phase": "label"})


def test_phase_label_requires_rip_completed():
    job = _miss_job(rip_state="running")
    with pytest.raises(StateViolation, match="rip_state"):
        validate_job_state_transition(job, {"phase": "label"})


def test_phase_postprocess_requires_rip_completed():
    """The postprocess phase requires a completed rip — this is the
    invariant Phase 2 will inherit (Option B's transfer-phase entry will
    also require completed rip)."""
    job = _hit_job(rip_state="running")
    with pytest.raises(StateViolation, match="rip_state"):
        validate_job_state_transition(job, {"phase": "postprocess"})


def test_phase_postprocess_on_miss_requires_label_completed():
    """On miss, postprocess additionally requires label_state completed.
    Phase 2 will preserve this — the unified transfer task still cannot
    start before labeling is done on the miss path."""
    job = _miss_job(rip_state="completed", label_state="ready")
    with pytest.raises(StateViolation, match="label_state"):
        validate_job_state_transition(job, {"phase": "postprocess"})


def test_phase_postprocess_on_hit_does_not_need_label():
    """On hit, label is skipped and postprocess can proceed once rip is done."""
    job = _hit_job(rip_state="completed", label_state="skipped")
    validate_job_state_transition(job, {"phase": "postprocess"})  # no raise


def test_phase_transfer_requires_rip_and_postprocess_completed():
    """The transfer phase requires both rip and post_state completed.

    **Post-3b note:** the validation now reads
    ``_next_derived_post_state`` rather than the column directly, so
    the "both gates clear" case needs to set fields the derivation
    actually consults. A job at "post completed, transitioning to
    transfer" in real life always has both ``post_state="completed"``
    AND ``transfer_state="ready"`` set together (see
    ``StageState.postprocess_complete``); the test mirrors that.
    """
    # Missing post_state — derivation falls through to "ready" (no
    # transfer_state set, no transfer_phase set) → still rejected.
    job = _hit_job(rip_state="completed", post_state="running")
    with pytest.raises(StateViolation, match="post_state"):
        validate_job_state_transition(job, {"phase": "transfer"})

    # Both gates clear: realistic post_complete state has post_state
    # "completed" AND transfer_state "ready" (StageState.postprocess_complete
    # writes both atomically). Either alone is enough for derived="completed"
    # under the post-3b semantics.
    ok = _hit_job(
        rip_state="completed", post_state="completed", transfer_state="ready",
    )
    validate_job_state_transition(ok, {"phase": "transfer"})  # no raise


def test_phase_finalize_release_requires_all_stages_completed():
    """finalize_release is the terminal phase; it requires rip, post, and
    transfer all completed. None of these prereqs are touched by the
    postprocess collapse, but capturing the rule keeps the test surface
    intact in case Phase 2 needs to re-derive any of them."""
    job = _hit_job(
        rip_state="completed", post_state="completed", transfer_state="running",
    )
    with pytest.raises(StateViolation, match="transfer_state"):
        validate_job_state_transition(job, {"phase": "finalize_release"})

    ok = _hit_job(
        rip_state="completed", post_state="completed", transfer_state="completed",
    )
    validate_job_state_transition(ok, {"phase": "finalize_release"})  # no raise

