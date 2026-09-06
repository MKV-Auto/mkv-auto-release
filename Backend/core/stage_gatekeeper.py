"""Stage admission gatekeeper — bounds concurrent heavy pipeline work (#863).

Queueing many jobs into postprocess+transfer used to dispatch them all to
Celery at once; worker concurrency (-c 5 per queue) then ran up to ten
multi-GB file movers in parallel and saturated the disk (2026-09-06: load
19, 82% iowait, and the I/O storm's role in the #862 outage).

This module keeps resources available *by construction*: jobs are admitted
into the two heavy stages FIFO, bounded by fixed slots:

- **prep** (the ``start_transfer`` worker's rename+hash phase,
  ``transfer_phase == "preparing"``): ``MAX_CONCURRENT_POSTPROCESS``, default 1
- **transfer** (``transfer_state == "running"``): ``MAX_CONCURRENT_TRANSFERS``,
  default 1 (enforced at the dispatch helpers / endpoint, alongside
  ``claim_transfer_for_dispatch``)

A job that is committed to run but finds no free slot gets
``jobs.dispatch_queued_at`` stamped and its stage state left untouched —
``ready`` already means "prerequisites met, awaiting trigger" per
docs/STATE_MACHINE.md, so no new stage value exists anywhere. The
``promote_queued_stages`` Celery task (workers/tasks.py) admits the next
queued job whenever a slot frees (completion/failure callbacks fire it; the
periodic zombie sweep is the straggler net). Counts are derived from job
rows, never from in-process counters, so they survive restarts.

Set a cap to 0 to disable that stage's limit entirely.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Job statuses that can hold a slot. 'failed' jobs never count against a cap
# (their transfer_phase/transfer_state may be frozen mid-flight forever).
_ACTIVE_JOB_STATUSES = ("pending", "running", "validating")


def _env_cap(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning("stage_gatekeeper: invalid %s=%r, using %d", name, raw, default)
        return default
    return value if value >= 0 else default


def max_concurrent_postprocess() -> int:
    """Prep-phase slot cap; 0 disables the limit."""
    return _env_cap("MAX_CONCURRENT_POSTPROCESS", 1)


def max_concurrent_transfers() -> int:
    """Transfer slot cap; 0 disables the limit."""
    return _env_cap("MAX_CONCURRENT_TRANSFERS", 1)


def _count_preparing(db: Any, exclude_job_id: Optional[str] = None) -> int:
    """Jobs actually HOLDING a prep slot.

    Rig-found (rc-1.6.14-rc.1): a job that failed mid-prep keeps a stale
    ``transfer_phase='preparing'``; the moment resume flips it back to
    running, that stale phase counted against the cap — the job queued
    BEHIND ITSELF and, filtered out of promotion, stranded forever. A
    genuinely admitted job always has ``dispatch_queued_at`` cleared, so a
    row only holds a slot when the marker is NULL — and the requester never
    counts against its own admission.
    """
    from api import models

    q = db.query(models.Job).filter(
        models.Job.transfer_phase == "preparing",
        models.Job.job_status.in_(_ACTIVE_JOB_STATUSES),
        models.Job.dispatch_queued_at.is_(None),
    )
    if exclude_job_id is not None:
        q = q.filter(models.Job.id != exclude_job_id)
    return q.count()


def _count_transferring(db: Any) -> int:
    from api import models

    return (
        db.query(models.Job)
        .filter(
            models.Job.transfer_state == "running",
            models.Job.job_status.in_(_ACTIVE_JOB_STATUSES),
        )
        .count()
    )


def postprocess_slot_available(db: Any, *, exclude_job_id: Optional[str] = None) -> bool:
    cap = max_concurrent_postprocess()
    return cap <= 0 or _count_preparing(db, exclude_job_id) < cap


def transfer_slot_available(db: Any) -> bool:
    cap = max_concurrent_transfers()
    return cap <= 0 or _count_transferring(db) < cap


def mark_queued(db: Any, job: Any, *, commit: bool = True) -> None:
    """Stamp the admission-queue marker (idempotent — keeps FIFO position)."""
    if getattr(job, "dispatch_queued_at", None) is None:
        job.dispatch_queued_at = datetime.now(timezone.utc)
        if commit:
            db.commit()


def request_pipeline_start(
    db: Any, job: Any, reason: str, *, apply_started: bool = True,
    **postprocess_started_kwargs: Any,
) -> str:
    """Admit-or-queue the unified ``start_transfer`` prep task.

    Replaces the bare ``start_transfer.delay(...)`` at every dispatch site.
    When a prep slot is free: clears the queue marker, applies
    ``StageState.postprocess_started`` (which sets
    ``transfer_phase='preparing'`` — the slot reservation itself, so the
    count is accurate from admission, not from worker start), then enqueues
    the task. When no slot is free: stamps ``dispatch_queued_at`` and leaves
    every stage state untouched; ``promote_queued_stages`` dispatches it
    when a slot frees.

    Returns ``"dispatched"`` or ``"queued"``.
    """
    if not postprocess_slot_available(db, exclude_job_id=str(job.id)):
        mark_queued(db, job)
        logger.info(
            "stage_gatekeeper: job %s queued for postprocess (cap=%d, reason=%s)",
            job.id, max_concurrent_postprocess(), reason,
        )
        return "queued"

    job.dispatch_queued_at = None
    if apply_started:
        from core.job_state import StageState

        StageState.postprocess_started(db, job, reason=reason, **postprocess_started_kwargs)
    else:
        # transfer/start endpoint parity (#365 — it never applied
        # postprocess_started; the worker sets transfer_phase itself). The
        # slot count under-reads for the enqueue→worker-start window only.
        db.commit()
    from workers.tasks import start_transfer as start_transfer_task

    start_transfer_task.delay(str(job.id))
    logger.info("stage_gatekeeper: job %s dispatched to start_transfer (%s)", job.id, reason)
    return "dispatched"


def next_queued_pipeline_job(db: Any) -> Optional[Any]:
    """Oldest job queued for the prep phase (FIFO by dispatch_queued_at)."""
    from sqlalchemy import or_

    from api import models

    # No transfer_phase filter: a resumed job can carry a STALE 'preparing'
    # from its failed attempt (rig-found, rc-1.6.14-rc.1) — queued rows are
    # identified by the marker alone, and postprocess_started overwrites the
    # phase on admission anyway.
    return (
        db.query(models.Job)
        .filter(
            models.Job.dispatch_queued_at.isnot(None),
            models.Job.job_status.in_(_ACTIVE_JOB_STATUSES),
            models.Job.rip_state.in_(("completed", "skipped")),
            or_(
                models.Job.transfer_state.is_(None),
                models.Job.transfer_state == "pending",
            ),
        )
        .order_by(models.Job.dispatch_queued_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def next_queued_transfer_job(db: Any) -> Optional[Any]:
    """Oldest job queued for the transfer phase (prep done, awaiting a slot)."""
    from api import models

    return (
        db.query(models.Job)
        .filter(
            models.Job.dispatch_queued_at.isnot(None),
            models.Job.transfer_state == "ready",
            models.Job.job_status.in_(_ACTIVE_JOB_STATUSES),
        )
        .order_by(models.Job.dispatch_queued_at.asc())
        .with_for_update(skip_locked=True)
        .first()
    )


def schedule_promotion(reason: str = "") -> None:
    """Fire-and-forget the promotion task; never raises into the caller."""
    try:
        from workers.tasks import promote_queued_stages

        promote_queued_stages.delay()
        logger.debug("stage_gatekeeper: promotion scheduled (%s)", reason)
    except Exception as exc:
        logger.warning("stage_gatekeeper: failed to schedule promotion (%s): %s", reason, exc)
