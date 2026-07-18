"""Shared query for the Ripper carousel's unfinished-job set.

The carousel shows two kinds of jobs:

- **Active**: ``rip_state in ('completed','skipped')`` AND ``job_status in
  ('running','validating','pending')`` — work-in-progress data exists on disk
  and the user needs an affordance to resume/retry the next stage. ``pending``
  surfaces workers that exited mid-flow (container restart, transfer crash,
  stuck post-process queue).
- **Failed (latest per disc)**: most recent failed job per disc, excluding any
  disc that already has a completed job (successful re-rip supersedes) or
  whose newest active job is newer than the failed one (in-flight retry).

Both the WS coordinator initial-state snapshot and the HTTP
``/jobs/unfinished/summaries`` endpoint consume this. Keeping it in one place
prevents the two from drifting (see #498 — they did drift, and pending/failed
cards vanished from the carousel on Ripper-page re-entry).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, joinedload

from api import models as db_models


def _norm_disc_hash(h: Any) -> str | None:
    if h is None:
        return None
    s = str(h).strip()
    return s.upper() if s else None


def query_unfinished_jobs(db: Session) -> list[db_models.Job]:
    """Return Job rows the carousel should display, newest-first within kind.

    Active jobs first, then failed jobs (latest per disc, unsuperseded).
    Eager-loads ``Job.disc -> Disc.release -> Release.movie`` so callers can
    project metadata without N+1 queries.
    """
    active_jobs = (
        db.query(db_models.Job)
        .options(
            joinedload(db_models.Job.disc)
                .joinedload(db_models.Disc.release)
                .joinedload(db_models.Release.movie),
        )
        .filter(
            db_models.Job.rip_state.in_(["completed", "skipped"]),
            db_models.Job.job_status.in_(["running", "validating", "pending"]),
            db_models.Job.dismissed.is_(False),  # #543
        )
        .order_by(db_models.Job.created_at.desc())
        .all()
    )

    active_newest_by_disc_id: dict[str, Any] = {}
    active_newest_by_hash: dict[str, Any] = {}
    for aj in active_jobs:
        created = getattr(aj, "created_at", None)
        if created is None:
            continue
        adisc = getattr(aj, "disc", None)
        if aj.disc_id:
            did = str(aj.disc_id)
            prev = active_newest_by_disc_id.get(did)
            if prev is None or created > prev:
                active_newest_by_disc_id[did] = created
        hkey = _norm_disc_hash(getattr(adisc, "content_hash", None) if adisc else None)
        if hkey:
            prev_h = active_newest_by_hash.get(hkey)
            if prev_h is None or created > prev_h:
                active_newest_by_hash[hkey] = created

    def _failed_superseded_by_newer_active(failed_job: db_models.Job) -> bool:
        fc = getattr(failed_job, "created_at", None)
        if fc is None:
            return False
        fdisc = getattr(failed_job, "disc", None)
        fid = getattr(failed_job, "disc_id", None)
        if fid:
            newest = active_newest_by_disc_id.get(str(fid))
            if newest is not None and newest > fc:
                return True
        fh = _norm_disc_hash(getattr(fdisc, "content_hash", None) if fdisc else None)
        if fh:
            newest_h = active_newest_by_hash.get(fh)
            if newest_h is not None and newest_h > fc:
                return True
        return False

    all_failed_jobs = (
        db.query(db_models.Job)
        .options(
            joinedload(db_models.Job.disc)
                .joinedload(db_models.Disc.release)
                .joinedload(db_models.Release.movie),
        )
        .filter(
            db_models.Job.job_status == "failed",
            db_models.Job.dismissed.is_(False),  # #543
        )
        .order_by(db_models.Job.created_at.desc())
        .all()
    )

    seen_disc_ids: set[str] = set()
    failed_jobs: list[db_models.Job] = []
    for job in all_failed_jobs:
        did = str(job.disc_id) if job.disc_id else str(job.id)
        if did not in seen_disc_ids:
            seen_disc_ids.add(did)
            failed_jobs.append(job)

    discs_with_completed_jobs: set[str] = set()
    if failed_jobs:
        disc_ids = {str(j.disc_id) for j in failed_jobs if j.disc_id}
        if disc_ids:
            discs_with_completed_jobs = {
                str(row[0])
                for row in db.query(db_models.Job.disc_id)
                .filter(
                    db_models.Job.disc_id.in_(disc_ids),
                    db_models.Job.job_status == "completed",
                )
                .all()
            }

    result: list[db_models.Job] = list(active_jobs)
    for job in failed_jobs:
        if (
            str(job.disc_id) not in discs_with_completed_jobs
            and not _failed_superseded_by_newer_active(job)
        ):
            result.append(job)
    return result
