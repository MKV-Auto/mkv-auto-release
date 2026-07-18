"""
Regression tests for #366: `job_status='validating'` leaking past postprocess
into transfer state, causing startup recovery to fail jobs that had already
successfully transferred (because the local transient/ was legitimately
cleaned up after transfer).

Three fixes, three integration cases:

1. `StageState.postprocess_complete` resets `validating → running` so the
   in-flight sub-state set by the worker during output validation does not
   leak past postprocess.

2. `_complete_transfer` for miss profile always sets `next_job_status =
   completed` (mirroring hit profile), so transfer success can never preserve
   `validating`.

3. `_recover_inflight_jobs` treats a job with `transfer_state='completed'`
   and `job_status='validating'` as a leaked state (defense in depth): clean
   it up to `completed` instead of trying to reconcile a vanished transient/.
"""
import uuid

import pytest

from api import models
from core.job_state import StageState


pytestmark = pytest.mark.integration


# --- Fix 1: postprocess_complete resets validating ---

def test_postprocess_complete_clears_validating(test_db):
    """When the worker leaves job_status='validating', postprocess_complete must
    reset it to 'running' so it does not leak past the postprocess phase."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-a", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="validating",
            rip_state="completed",
            phase="postprocess",
            transfer_state="pending",
            stage_profile="miss",
            rip_progress=100,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        StageState.postprocess_complete(
            session,
            job,
            post_paths={"title-1": "/tmp/out/file.mkv"},
        )
        session.refresh(job)

        # #365 step 5 — post_state column dropped; use derived hybrid_property.
        assert job.derived_post_state == "completed"
        assert job.phase == "transfer"
        assert job.transfer_state == "ready"
        assert job.job_status == "running", (
            "postprocess_complete must reset job_status from 'validating' to "
            "'running' to prevent leak into transfer state (#366)"
        )


def test_postprocess_complete_preserves_running_status(test_db):
    """The reset is conditional on validating — running status passes through."""
    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-b", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="running",
            rip_state="completed",
            phase="postprocess",
            transfer_state="pending",
            stage_profile="miss",
            rip_progress=100,
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        StageState.postprocess_complete(
            session,
            job,
            post_paths={"title-1": "/tmp/out/file.mkv"},
        )
        session.refresh(job)
        assert job.job_status == "running"


# --- Fix 3: _complete_transfer for miss profile sets completed ---

def test_complete_transfer_miss_profile_sanitizes_validating_to_running(test_db):
    """Miss-profile transfer success with pending finalize stages must never
    preserve in-flight job_status='validating' — sanitize it to 'running' so it
    cannot bleed into startup recovery as a local-reconciliation candidate.

    (We do not force 'completed' here because miss profile may still have
    pending finalize_release; the strong invariant correctly prevents that.)"""
    from api.routers.jobs import _complete_transfer

    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-c", disc_number=1)
        session.add(disc)
        session.flush()
        # Simulate the bug-prone state: job_status still 'validating' going into
        # transfer completion (e.g. because postprocess_complete didn't reset it
        # under an older version of the code).
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="validating",
            rip_state="completed",
            phase="transfer",
            transfer_state="running",
            stage_profile="miss",
            rip_progress=100,
            post_paths={"title-1": "/tmp/out/file.mkv"},
            # Realistic miss-profile state at this point (finalize_release pending)
            scan_state="completed",
            label_state="completed",
            finalize_state="ready",
            finalize_release_state="pending",
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        _complete_transfer(
            job,
            session,
            dest_paths=["/dest/file.mkv"],
            job_metadata={},
        )
        session.refresh(job)

        assert job.transfer_state == "completed"
        assert job.job_status != "validating", (
            "miss-profile transfer success must never preserve 'validating' "
            "(#366 root cause); it should be 'running' (no pending finalize) "
            "or 'completed' (all stages done)"
        )
        assert job.job_status == "running"
        assert job.phase == "complete"


def test_complete_transfer_hit_profile_sanitizes_validating_to_completed(test_db):
    """Hit-profile transfer success goes through the `next_job_status='completed'`
    branch; a stale in-flight 'validating' must end up 'completed', never
    surviving into startup recovery."""
    from api.routers.jobs import _complete_transfer

    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-e", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="validating",
            rip_state="completed",
            phase="transfer",
            transfer_state="running",
            stage_profile="hit",
            rip_progress=100,
            post_paths={"title-1": "/tmp/out/file.mkv"},
            scan_state="completed",
            label_state="skipped",
            finalize_state="skipped",
        )
        session.add(job)
        session.commit()
        session.refresh(job)

        _complete_transfer(
            job,
            session,
            dest_paths=["/dest/file.mkv"],
            job_metadata={},
        )
        session.refresh(job)

        assert job.transfer_state == "completed"
        assert job.job_status == "completed", (
            "hit-profile transfer success must finish the job as 'completed' "
            "even when entered with a stale 'validating' sub-state (#366)"
        )
        assert job.phase == "complete"


# --- Fix 2: _recover_inflight_jobs clears stale validating ---

def test_recover_inflight_clears_stale_validating_when_transfer_completed(
    test_db, monkeypatch
):
    """A job already-transferred but still flagged job_status='validating' (the
    exact #366 leaked state from a buggy older version) must be cleared by
    recovery (sanitized to 'running') instead of failed via local-reconciliation
    against a transient/ that transfer has already cleaned up."""
    from api import main as api_main

    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-d", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="validating",
            rip_state="completed",
            phase="complete",
            transfer_state="completed",
            stage_profile="miss",
            rip_progress=100,
            post_paths={"title-1": "/tmp/out/file.mkv"},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)

    # Guard: the local validation path must NOT be invoked for this case.
    called = {"gather_final_outputs": 0}

    def _trap(*args, **kwargs):
        called["gather_final_outputs"] += 1
        raise AssertionError(
            "gather_final_outputs must not be called for a job that already has "
            "transfer_state='completed' (#366 defense in depth)"
        )

    monkeypatch.setattr(api_main, "gather_final_outputs", _trap)
    # Recovery uses both _fail_orphaned_rip_jobs_on_startup and the rip recovery
    # path; stub the orphan-rip check so we focus on the validating-recovery branch.
    monkeypatch.setattr(
        "api.routers.jobs._fail_orphaned_rip_jobs_on_startup",
        lambda db: [],
    )

    api_main._recover_inflight_jobs()

    with test_db() as session:
        refreshed = session.query(models.Job).filter(models.Job.id == job_id).first()
        assert refreshed is not None
        assert refreshed.job_status == "running", (
            "stale validating + transfer completed must be cleared to 'running' "
            "by recovery (not failed via local reconciliation) (#366)"
        )
        assert called["gather_final_outputs"] == 0


def test_recover_inflight_transfer_completed_survives_missing_job_dir(
    test_db, monkeypatch
):
    """Locks in branch ordering inside _recover_inflight_jobs: the
    transfer_state='completed' sanitize branch must `continue` BEFORE the
    "job dir missing" branch, so an already-transferred job whose working
    directory was legitimately purged is never failed with
    'Recovery skipped: working directory missing' (#366 acceptance criterion 3)."""
    from api import main as api_main
    from core.job_paths import JobPaths

    with test_db() as session:
        disc = models.Disc(id=str(uuid.uuid4()), content_hash="h366-f", disc_number=1)
        session.add(disc)
        session.flush()
        job = models.Job(
            disc_id=disc.id,
            disc_num="1",
            mount_point="/mnt/dvd",
            job_status="validating",
            rip_state="completed",
            phase="complete",
            transfer_state="completed",
            stage_profile="miss",
            rip_progress=100,
            post_paths={"title-1": "/tmp/out/file.mkv"},
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = str(job.id)

    # Precondition for the scenario: the job working directory does not exist
    # (transfer already cleaned up / data dir purged after successful transfer).
    assert not JobPaths.for_id(job_id).root.exists()

    monkeypatch.setattr(
        "api.routers.jobs._fail_orphaned_rip_jobs_on_startup",
        lambda db: [],
    )

    api_main._recover_inflight_jobs()

    with test_db() as session:
        refreshed = session.query(models.Job).filter(models.Job.id == job_id).first()
        assert refreshed is not None
        assert refreshed.job_status == "running", (
            "already-transferred job with purged job dir must be sanitized to "
            "'running', not failed (#366)"
        )
        assert refreshed.job_status != "failed"
        assert not (refreshed.error_reason or "").startswith("Recovery skipped"), (
            "the transfer-completed sanitize branch must run before the "
            "job-dir-missing failure branch (#366)"
        )
