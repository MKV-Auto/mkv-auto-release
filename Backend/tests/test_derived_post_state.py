"""
Coverage for the ``Job.derived_post_state`` hybrid_property.

This property is **step 1 of 5** in the ``post_state`` column drop
workstream (#365 follow-up after the transient/-drop completed in
#460). It derives ``post_state`` from the rest of the job state
(``rip_state`` + ``label_state`` + ``transfer_phase`` +
``transfer_state`` + ``job_status``) so callers can be migrated off
the explicit column one at a time before the column itself is
dropped.

The tests below walk every position of the canonical lifecycle and
also exercise the off-the-rails cases (rip failed, postprocess
failed, transfer failed) to confirm the decision-table ordering is
correct.

**Parity** between ``derived_post_state`` and the actual
``Job.post_state`` column is **not asserted** here — the whole point
of deriving is that the column will eventually go away, and the
derivation logic is the new source of truth. Callers being migrated
in later PRs should switch their reads from ``job.post_state`` to
``job.derived_post_state`` once the derivation has been validated
against their specific code path.
"""
from api.models import Job


def _job(**kw) -> Job:
    """Build a Job instance with the given attributes set. Defaults
    leave the other state fields at None so the derivation walks the
    early-return branches naturally."""
    j = Job()
    for key, value in kw.items():
        setattr(j, key, value)
    return j


# ──────────────────────────────────────────────────────────────────────────
# Pre-rip — postprocess hasn't entered the picture
# ──────────────────────────────────────────────────────────────────────────


def test_just_created_returns_none():
    """Fresh job: rip_state=None → None (postprocess N/A)."""
    assert _job().derived_post_state is None


def test_rip_running_returns_none():
    """Rip in flight: same as just-created — None."""
    assert _job(rip_state="running", job_status="running").derived_post_state is None


def test_rip_failed_returns_none():
    """Rip failure pre-empts postprocess. The job is rip-failed, not
    postprocess-failed; the derived state correctly reports None
    rather than falsely surfacing a "failed" postprocess state that
    the column also wouldn't show."""
    assert _job(rip_state="failed", job_status="failed").derived_post_state is None


# ──────────────────────────────────────────────────────────────────────────
# Post-rip, pre-transfer — branch-aware ready/pending
# ──────────────────────────────────────────────────────────────────────────


def test_hit_branch_ready_after_rip():
    """Hit profile: rip_complete sets ``label_state="skipped"``,
    ``post_state="ready"``. Derivation matches: skipped label → ready."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state=None,
        transfer_phase=None,
        job_status="running",
    )
    assert j.derived_post_state == "ready"


def test_miss_branch_pending_before_label():
    """Miss profile: rip_complete sets ``label_state="ready"``,
    ``post_state="pending"`` (operator must label before postprocess
    can start). Derivation: non-completed/skipped label → pending."""
    j = _job(
        rip_state="completed",
        label_state="ready",
        transfer_state=None,
        transfer_phase=None,
        job_status="running",
    )
    assert j.derived_post_state == "pending"


def test_miss_branch_ready_after_label_completed():
    """Miss profile after label completes:
    ``label_state="completed"`` → ready (postprocess can start)."""
    j = _job(
        rip_state="completed",
        label_state="completed",
        transfer_state=None,
        transfer_phase=None,
        job_status="running",
    )
    assert j.derived_post_state == "ready"


def test_rip_skipped_treated_same_as_completed():
    """``rip_state="skipped"`` (rare; e.g. labeling-only jobs) is
    treated as a rip-done signal for derivation purposes — same as
    ``"completed"``."""
    j = _job(
        rip_state="skipped",
        label_state="skipped",
        job_status="running",
    )
    assert j.derived_post_state == "ready"


# ──────────────────────────────────────────────────────────────────────────
# Postprocess in flight — the "preparing" sub-phase
# ──────────────────────────────────────────────────────────────────────────


def test_preparing_sub_phase_is_running():
    """Collapsed-model invariant: ``transfer_phase="preparing"`` IS
    the postprocess running phase. The whole point of the
    transient/-drop architecture was unifying these."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="pending",
        transfer_phase="preparing",
        job_status="running",
    )
    assert j.derived_post_state == "running"


def test_validating_job_status_during_preparing_is_running():
    """The worker sets ``job_status="validating"`` during postprocess
    output validation (still inside the preparing sub-phase). The
    job-failed early-return guard at step 2 of the decision table is
    keyed on "failed", not "validating", so this correctly returns
    running."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="pending",
        transfer_phase="preparing",
        job_status="validating",
    )
    assert j.derived_post_state == "running"


# ──────────────────────────────────────────────────────────────────────────
# Postprocess complete — past the preparing sub-phase
# ──────────────────────────────────────────────────────────────────────────


def test_transferring_sub_phase_means_post_complete():
    """``transfer_phase="transferring"`` → we've left preparing, so
    postprocess must be done."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="running",
        transfer_phase="transferring",
        job_status="running",
    )
    assert j.derived_post_state == "completed"


def test_verifying_sub_phase_means_post_complete():
    """``transfer_phase="verifying"`` — same as transferring,
    postprocess is done. This also covers the src==dest shortcut path
    (#454) which jumps straight to verifying after the rename."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="running",
        transfer_phase="verifying",
        job_status="running",
    )
    assert j.derived_post_state == "completed"


def test_transfer_state_ready_means_post_complete():
    """Postprocess complete sets ``transfer_state="ready"`` and
    leaves ``transfer_phase=null`` (transfer task hasn't started
    yet). Decision-table step 5 catches this."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="ready",
        transfer_phase=None,
        job_status="running",
    )
    assert j.derived_post_state == "completed"


def test_transfer_completed_means_post_complete():
    """``transfer_state="completed"`` → postprocess definitely done."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="completed",
        transfer_phase=None,
        job_status="completed",
    )
    assert j.derived_post_state == "completed"


def test_transfer_failed_still_means_post_complete():
    """If transfer started and then failed, postprocess succeeded
    earlier (rename + hash + validate must have passed for transfer
    to have started). The derivation correctly reports postprocess
    completed even though the overall job is in a transfer-failed
    state."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="failed",
        transfer_phase=None,
        job_status="running",  # transfer-failed leaves job_status=running
    )
    assert j.derived_post_state == "completed"


# ──────────────────────────────────────────────────────────────────────────
# Postprocess failed — distinct from rip-failed and transfer-failed
# ──────────────────────────────────────────────────────────────────────────


def test_postprocess_failed_before_preparing_marker():
    """``StageState.postprocess_failed`` sets ``job_status="failed"``
    but doesn't necessarily touch transfer_phase. If postprocess
    failed before the preparing marker was set (e.g. rename_outputs
    crashed at a setup step), derivation must still report failed."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state=None,
        transfer_phase=None,
        job_status="failed",
    )
    assert j.derived_post_state == "failed"


def test_postprocess_failed_during_preparing():
    """Postprocess failed while transfer_phase was already set to
    preparing. job_status=failed + transfer_state not failed/
    completed → failed."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="pending",
        transfer_phase="preparing",
        job_status="failed",
    )
    assert j.derived_post_state == "failed"


def test_transfer_failure_not_misreported_as_post_failed():
    """If the job is in transfer-failed (transfer_state=failed) the
    derivation must NOT report it as a postprocess failure. Decision
    table step 2's guard excludes transfer_state=failed for exactly
    this reason."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="failed",
        transfer_phase=None,
        job_status="failed",
    )
    # transfer_state="failed" places this in the "completed" bucket
    # per step 5 — postprocess succeeded, transfer is what failed.
    assert j.derived_post_state == "completed"


def test_completed_transfer_not_misreported_as_post_failed():
    """Job completed normally (rare to see job_status=failed at the
    same time, but defensively): transfer_state=completed should
    classify postprocess as completed, never failed."""
    j = _job(
        rip_state="completed",
        label_state="skipped",
        transfer_state="completed",
        transfer_phase=None,
        job_status="failed",  # hypothetical inconsistency
    )
    assert j.derived_post_state == "completed"


# ──────────────────────────────────────────────────────────────────────────
# Sanity: the property is callable on a freshly-constructed Job
# ──────────────────────────────────────────────────────────────────────────


def test_property_is_accessible_on_unattached_instance():
    """The hybrid_property works on a Job instance that's never been
    added to a session. This matters for tests + transient
    constructions where attaching to a session is overkill."""
    j = Job()
    # Should not raise — and returns None by the rip_state guard.
    assert j.derived_post_state is None
