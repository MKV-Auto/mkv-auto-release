"""#863: stage admission gatekeeper — bounded concurrent postprocess/transfer.

Queueing ~20 jobs used to dispatch them all at once (worker -c 5 per queue
ran up to ten parallel multi-GB movers; prod 2026-09-06: load 19, 82%
iowait). Jobs are now admitted FIFO into fixed slots; the rest wait with
``dispatch_queued_at`` stamped and stage states untouched.
"""
from __future__ import annotations

import datetime
import uuid

import pytest

from api import models
from core import stage_gatekeeper as gk


def _mk_job(session, *, job_status="running", rip_state="completed",
            label_state="skipped", transfer_phase=None, transfer_state=None,
            queued_at=None, phase="postprocess"):
    disc = models.Disc(
        id=str(uuid.uuid4()),
        content_hash=f"hash-{uuid.uuid4().hex[:16]}",
    )
    session.add(disc)
    session.flush()
    job = models.Job(
        id=str(uuid.uuid4()),
        disc_id=disc.id,
        disc_num="0",
        mount_point="/dev/sr0",
        job_status=job_status,
        rip_state=rip_state,
        label_state=label_state,
        transfer_phase=transfer_phase,
        transfer_state=transfer_state,
        dispatch_queued_at=queued_at,
        phase=phase,
    )
    session.add(job)
    session.commit()
    return job


@pytest.fixture
def dispatched(monkeypatch):
    """Record start_transfer dispatches instead of hitting Celery."""
    calls: list[str] = []
    import workers.tasks as wt

    monkeypatch.setattr(wt.start_transfer, "delay", lambda job_id: calls.append(job_id))
    return calls


def test_env_caps(monkeypatch):
    monkeypatch.delenv("MAX_CONCURRENT_POSTPROCESS", raising=False)
    assert gk.max_concurrent_postprocess() == 1
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "3")
    assert gk.max_concurrent_postprocess() == 3
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "0")
    assert gk.max_concurrent_postprocess() == 0
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "bogus")
    assert gk.max_concurrent_postprocess() == 1
    monkeypatch.setenv("MAX_CONCURRENT_TRANSFERS", "-2")
    assert gk.max_concurrent_transfers() == 1


def test_free_slot_dispatches_and_reserves(test_db, dispatched, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "1")
    with test_db() as db:
        job = _mk_job(db)
        assert gk.request_pipeline_start(db, job, "test") == "dispatched"
        db.refresh(job)
        # The reservation IS the transfer_phase flip — counts are accurate
        # from admission, not from worker start.
        assert job.transfer_phase == "preparing"
        assert job.dispatch_queued_at is None
        assert dispatched == [str(job.id)]


def test_busy_slot_queues_without_touching_state(test_db, dispatched, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "1")
    with test_db() as db:
        _mk_job(db, transfer_phase="preparing")  # occupies the slot
        job = _mk_job(db)
        assert gk.request_pipeline_start(db, job, "test") == "queued"
        db.refresh(job)
        assert job.transfer_phase is None
        assert job.dispatch_queued_at is not None
        assert dispatched == []
        # Re-request keeps the original FIFO position.
        first_stamp = job.dispatch_queued_at
        assert gk.request_pipeline_start(db, job, "again") == "queued"
        db.refresh(job)
        assert job.dispatch_queued_at == first_stamp


def test_cap_zero_disables_the_limit(test_db, dispatched, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "0")
    with test_db() as db:
        for _ in range(4):
            _mk_job(db, transfer_phase="preparing")
        job = _mk_job(db)
        assert gk.request_pipeline_start(db, job, "test") == "dispatched"
        assert dispatched == [str(job.id)]


def test_failed_jobs_never_hold_a_slot(test_db, dispatched, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "1")
    with test_db() as db:
        _mk_job(db, transfer_phase="preparing", job_status="failed")
        job = _mk_job(db)
        assert gk.request_pipeline_start(db, job, "test") == "dispatched"


def test_pipeline_queue_is_fifo_and_filtered(test_db):
    now = datetime.datetime.now(datetime.timezone.utc)
    with test_db() as db:
        newer = _mk_job(db, queued_at=now)
        older = _mk_job(db, queued_at=now - datetime.timedelta(minutes=5))
        # Not candidates: already ready for transfer, or failed. (A stale
        # 'preparing' phase does NOT disqualify — see the stale-phase tests.)
        _mk_job(db, queued_at=now - datetime.timedelta(hours=1), transfer_state="ready")
        _mk_job(db, queued_at=now - datetime.timedelta(hours=1), job_status="failed")
        candidate = gk.next_queued_pipeline_job(db)
        assert candidate is not None and candidate.id == older.id


def test_resume_with_stale_preparing_phase_admits_itself(test_db, dispatched, monkeypatch):
    """Rig-found (rc-1.6.14-rc.1): a job that failed mid-prep keeps
    transfer_phase='preparing'; on resume it counted against the cap and
    queued BEHIND ITSELF on an otherwise idle system."""
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "1")
    with test_db() as db:
        job = _mk_job(db, transfer_phase="preparing")  # stale, from the failed run
        assert gk.request_pipeline_start(db, job, "resume") == "dispatched"
        assert dispatched == [str(job.id)]


def test_stale_preparing_queued_job_is_promotable(test_db):
    """The promotion candidate filter must not exclude queued jobs whose
    failed attempt left transfer_phase='preparing' — they stranded forever."""
    now = datetime.datetime.now(datetime.timezone.utc)
    with test_db() as db:
        stuck = _mk_job(db, queued_at=now, transfer_phase="preparing")
        candidate = gk.next_queued_pipeline_job(db)
        assert candidate is not None and candidate.id == stuck.id
        # And while queued (marker set), the stale phase holds no slot.
        assert gk.postprocess_slot_available(db) is True


def test_transfer_slot_counting_and_queue(test_db, monkeypatch):
    monkeypatch.setenv("MAX_CONCURRENT_TRANSFERS", "1")
    now = datetime.datetime.now(datetime.timezone.utc)
    with test_db() as db:
        assert gk.transfer_slot_available(db) is True
        _mk_job(db, transfer_state="running")
        assert gk.transfer_slot_available(db) is False
        older = _mk_job(db, transfer_state="ready", queued_at=now - datetime.timedelta(minutes=2))
        _mk_job(db, transfer_state="ready", queued_at=now)
        candidate = gk.next_queued_transfer_job(db)
        assert candidate is not None and candidate.id == older.id


def test_promotion_admits_next_queued_prep(test_db, dispatched, monkeypatch):
    """End-to-end: slot busy → job queued → slot frees → promotion dispatches."""
    monkeypatch.setenv("MAX_CONCURRENT_POSTPROCESS", "1")
    monkeypatch.setenv("MAX_CONCURRENT_TRANSFERS", "1")
    import workers.tasks as wt

    with test_db() as db:
        blocker = _mk_job(db, transfer_phase="preparing")
        queued = _mk_job(db)
        assert gk.request_pipeline_start(db, queued, "test") == "queued"
        blocker_id, queued_id = str(blocker.id), str(queued.id)

    # Prep finishes: transfer_phase clears (what postprocess_complete does).
    with test_db() as db:
        blocker = db.query(models.Job).filter(models.Job.id == blocker_id).first()
        blocker.transfer_phase = None
        blocker.transfer_state = "completed"
        db.commit()

    wt.promote_queued_stages.apply()

    with test_db() as db:
        queued = db.query(models.Job).filter(models.Job.id == queued_id).first()
        assert queued.transfer_phase == "preparing"
        assert queued.dispatch_queued_at is None
    assert dispatched == [queued_id]


def test_card_state_shows_queued(test_db):
    from core.card_state import derive_card_state

    now = datetime.datetime.now(datetime.timezone.utc)
    with test_db() as db:
        queued = _mk_job(db, transfer_state="ready", queued_at=now)
        d = derive_card_state(queued)
        assert d["card_state"] == "stage_queued"
        assert d["family"] == "working"
        assert d["pill"] == "Queued"
        # Admitted (marker cleared): back to the normal your-turn contract.
        queued.dispatch_queued_at = None
        db.commit()
        d = derive_card_state(queued, transfer_destination=True)
        assert d["card_state"] == "ready_to_transfer"
