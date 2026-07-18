"""
Drive Gatekeeper - Single source of truth for drive/disc state and operations.

This module provides the DriveGatekeeper class which:
- Owns all drive/disc state (stored in Postgres, not in-memory)
- Is the single entry point for all rip operations
- Prevents duplicates at the gate (only duplicate check needed)
- Handles hash-based detection for already-scanned discs
- Includes recovery mechanisms for failed info scans
- Is the ONLY thing that can modify rip state
"""
import json
import logging
import time
from typing import Optional, Tuple, Dict, Any
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from sqlalchemy import or_
from sqlalchemy.exc import OperationalError

from api import models as db_models
from api import crud
from core.disc_manager import get_cached_discs
# Lock imports removed - rip concurrency now handled by Celery+PID checks
from core.drive_manager_client import DriveManagerError
from workers.tasks import rip_disc, celery_app
from core.job_state import apply_job_state, StageState

logger = logging.getLogger("core.drive_gatekeeper")


def is_pid_alive(pid: int) -> bool:
    """
    Check if a process PID is alive.

    Uses psutil when available; falls back to ``os.kill(pid, 0)`` (POSIX
    semantics — returns silently if the PID exists, raises ProcessLookupError
    if not) when psutil isn't installed. The fallback path is critical because
    some container builds ship without psutil, and a silent ``return False``
    here breaks every call site that depends on truthful liveness — most
    visibly the rip_disc revoke handler (#544) which early-returns and
    leaves the makemkvcon subprocess orphaned.

    Args:
        pid: Process ID to check

    Returns:
        True if process is alive, False otherwise
    """
    if not pid or pid <= 0:
        return False
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        # psutil missing — use POSIX kill(pid, 0).
        import os
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # PID exists but is owned by a different user — still "alive"
            # for our purposes.
            return True
        except OSError:
            return False
    except Exception:
        return False


def is_rip_task_active(job: db_models.Job) -> bool:
    """
    Check if a rip task is active by:
    1. Checking if job is in a failed state (if so, it's not active unless PID is alive)
    2. Checking Celery task state (active/reserved/scheduled)
    3. Checking if PID is alive (if rip_pid is set)
    
    Returns True if job is not failed AND (Celery says task is active OR PID is alive).
    
    Args:
        job: Job model instance
        
    Returns:
        True if rip task is active, False otherwise
    """
    # First check: If job is marked as failed in database, it's not active
    # (even if Celery task is still in PENDING/STARTED state)
    job_status = getattr(job, "job_status", None)
    rip_state = getattr(job, "rip_state", None)
    if job_status == "failed" or rip_state == "failed":
        # Only consider it active if there's an actual running PID
        # (recovery scenario where job was marked failed but process is still running)
        rip_pid = getattr(job, "rip_pid", None)
        if rip_pid and is_pid_alive(rip_pid):
            logger.debug(
                "is_rip_task_active job=%s is failed but PID %s is alive -> active (recovery scenario)",
                job.id, rip_pid
            )
            return True
        else:
            logger.debug(
                "is_rip_task_active job=%s is failed and no active PID -> inactive",
                job.id
            )
            return False
    
    # Check Celery state
    if job.celery_task_id:
        try:
            from celery.result import AsyncResult
            task_result = AsyncResult(job.celery_task_id, app=celery_app)
            task_state = task_result.state
            if task_state in ('PENDING', 'STARTED', 'RETRY'):
                logger.debug(
                    "is_rip_task_active job=%s celery_task_id=%s state=%s -> active",
                    job.id, job.celery_task_id, task_state
                )
                return True
        except Exception as exc:
            # If Celery check fails, log warning but continue to PID check
            logger.warning(
                "is_rip_task_active failed to check Celery state for job %s: %s",
                job.id, exc
            )
    
    # Check PID liveness
    if job.rip_pid:
        if is_pid_alive(job.rip_pid):
            logger.debug(
                "is_rip_task_active job=%s rip_pid=%s -> active (PID alive)",
                job.id, job.rip_pid
            )
            return True
    
    logger.debug("is_rip_task_active job=%s -> inactive", job.id)
    return False


def is_rip_task_really_running(job: db_models.Job) -> bool:
    """
    Like is_rip_task_active but treats PENDING as inactive.
    Use when deciding whether to re-dispatch after e.g. backend restart:
    a task stuck as PENDING in the result backend (worker never ran it or lost it)
    should not block re-dispatch.
    """
    job_status = getattr(job, "job_status", None)
    rip_state = getattr(job, "rip_state", None)
    if job_status == "failed" or rip_state == "failed":
        rip_pid = getattr(job, "rip_pid", None)
        if rip_pid and is_pid_alive(rip_pid):
            return True
        return False
    if job.celery_task_id:
        try:
            from celery.result import AsyncResult
            task_result = AsyncResult(job.celery_task_id, app=celery_app)
            task_state = task_result.state
            # Only STARTED/RETRY count as really running; PENDING means not yet picked up (or stale)
            if task_state in ("STARTED", "RETRY"):
                return True
        except Exception:
            pass
    if getattr(job, "rip_pid", None) and is_pid_alive(job.rip_pid):
        return True
    return False


def is_rip_running_for_disc(
    db: Session,
    disc_hash: Optional[str],
    disc_num: str,
    mount_point: str,
) -> Tuple[bool, Optional[db_models.Job]]:
    """
    UNIFIED PIPELINE: Single source of truth for determining if a rip is actually running.
    
    This function consolidates ALL checks into one authoritative pipeline:
    1. Check if makemkvcon process is running (fastest, most reliable)
    2. Check all jobs for this disc (by disc_hash or mount_point) for active Celery tasks or PIDs
    3. Check database state (job_status/rip_state) as fallback
    
    Args:
        db: Database session
        disc_hash: Disc content hash (preferred identifier)
        disc_num: Disc number
        mount_point: Mount point of the disc
        
    Returns:
        Tuple of (is_running: bool, active_job: Optional[Job])
        - If is_running is True, active_job contains the job that's running
        - If is_running is False, active_job is None
    """
    # Check 1: Fastest check - is makemkvcon process running?
    try:
        from core.utils import _is_makemkvcon_running_for_disc
        process_running = _is_makemkvcon_running_for_disc(
            mount_point,
            makemkv_disc_index=str(disc_num).strip() if str(disc_num or "").strip() else None,
        )
        logger.debug(
            "is_rip_running_for_disc Check 1: process check disc_num=%s mount_point=%s disc_hash=%s process_running=%s",
            disc_num, mount_point, disc_hash, process_running
        )
        if process_running:
            # Process is running - find the associated job
            # Try to find job by mount_point first (works even without disc_hash)
            active_job = (
                db.query(db_models.Job)
                .filter(
                    db_models.Job.mount_point == mount_point,
                    or_(
                        db_models.Job.job_status.in_(["pending", "running"]),
                        db_models.Job.rip_state.in_(["pending", "running"]),
                    ),
                )
                .order_by(db_models.Job.created_at.desc())
                .first()
            )
            if active_job:
                return True, active_job
            
            # If no job found by mount_point, try by disc_hash
            if disc_hash:
                active_job = (
                    db.query(db_models.Job)
                    .join(db_models.Disc, db_models.Job.disc_id == db_models.Disc.id)
                    .filter(
                        db_models.Disc.content_hash == disc_hash,
                        or_(
                            db_models.Job.job_status.in_(["pending", "running"]),
                            db_models.Job.rip_state.in_(["pending", "running"]),
                        ),
                    )
                    .order_by(db_models.Job.created_at.desc())
                    .first()
                )
                if active_job:
                    return True, active_job
            
            # Process is running but no job found - this is unusual but process check is authoritative
            logger.warning(
                "is_rip_running_for_disc: makemkvcon process detected but no associated job found "
                "disc_num=%s mount_point=%s disc_hash=%s",
                disc_num, mount_point, disc_hash
            )
            return True, None
    except Exception as proc_check_exc:
        logger.warning(
            "is_rip_running_for_disc: Failed to check makemkvcon process disc_num=%s mount_point=%s: %s",
            disc_num, mount_point, proc_check_exc
        )
        # Continue to other checks if process check fails
    
    # Check 2: Check all jobs for this disc for active Celery tasks or PIDs
    # This catches cases where process check might miss something or job exists but process isn't detected yet
    use_mount_point_check = not disc_hash or disc_hash.startswith("pending-")
    
    if use_mount_point_check:
        # Check by mount_point if disc_hash is not available
        candidate_jobs = (
            db.query(db_models.Job)
            .filter(
                db_models.Job.mount_point == mount_point,
                or_(
                    db_models.Job.job_status.in_(["pending", "running"]),
                    db_models.Job.rip_state.in_(["pending", "running"]),
                ),
            )
            .order_by(db_models.Job.created_at.desc())
            .all()
        )
    else:
        # Check by disc_hash (normal case)
        candidate_jobs = (
            db.query(db_models.Job)
            .join(db_models.Disc, db_models.Job.disc_id == db_models.Disc.id)
            .filter(
                db_models.Disc.content_hash == disc_hash,
                or_(
                    db_models.Job.job_status.in_(["pending", "running"]),
                    db_models.Job.rip_state.in_(["pending", "running"]),
                ),
            )
            .order_by(db_models.Job.created_at.desc())
            .all()
        )
    
    # Check each candidate job to see if it's actually active
    logger.debug(
        "is_rip_running_for_disc Check 2: checking %d candidate jobs disc_num=%s mount_point=%s disc_hash=%s",
        len(candidate_jobs), disc_num, mount_point, disc_hash
    )
    for job in candidate_jobs:
        is_active = is_rip_task_active(job)
        logger.debug(
            "is_rip_running_for_disc Check 2: job_id=%s job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s is_active=%s",
            job.id, job.job_status, getattr(job, "rip_state", None),
            getattr(job, "celery_task_id", None), getattr(job, "rip_pid", None), is_active
        )
        if is_active:
            logger.debug(
                "is_rip_running_for_disc: Active job found disc_num=%s mount_point=%s disc_hash=%s job_id=%s",
                disc_num, mount_point, disc_hash, job.id
            )
            return True, job
    
    # Check 3: Fallback - check database state even if process/task checks didn't find anything
    # If DB says running but process/task checks say inactive, this is likely a stale job
    # We return it but mark it for reconciliation (caller should handle stale job reconciliation)
    if candidate_jobs:
        # We found jobs in pending/running state but process/task checks said inactive
        # This could be a race condition or stale state
        most_recent = candidate_jobs[0]
        rip_state = getattr(most_recent, "rip_state", None)
        job_status = most_recent.job_status
        
        # Only return True if job is actually in running state (not just pending)
        # Pending jobs might not have started yet, so we should allow new rips
        if job_status == "running" or rip_state == "running":
            logger.warning(
                "is_rip_running_for_disc: Found job in running state but process/task inactive (stale job detected) "
                "disc_num=%s mount_point=%s disc_hash=%s job_id=%s job_status=%s rip_state=%s. "
                "Caller should reconcile this stale job.",
                disc_num, mount_point, disc_hash, most_recent.id,
                job_status, rip_state
            )
            return True, most_recent
        elif job_status == "pending":
            # Job is pending but not active - might be queued or stuck
            # Don't block new rips for pending jobs that aren't active
            logger.debug(
                "is_rip_running_for_disc: Found pending job but process/task inactive "
                "disc_num=%s mount_point=%s disc_hash=%s job_id=%s. Not blocking new rip.",
                disc_num, mount_point, disc_hash, most_recent.id
            )
            return False, None
    
    # No active rip found
    logger.debug(
        "is_rip_running_for_disc FINAL: No active rip found disc_num=%s mount_point=%s disc_hash=%s. Returning False, None",
        disc_num, mount_point, disc_hash
    )
    return False, None


def get_disc_info(
    disc_hash: Optional[str],
    disc_num: str,
    mount_point: str,
    refresh: bool = False,
    db: Optional[Session] = None,
) -> Dict[str, Any]:
    """
    Compatibility wrapper for tests that patch module-level get_disc_info.
    """
    if db is None:
        raise RuntimeError("DriveGatekeeper.get_disc_info requires a database session")
    gatekeeper = DriveGatekeeper(db)
    return gatekeeper.get_disc_info(disc_hash, disc_num, mount_point, refresh=refresh)


class DriveGatekeeper:
    """Single source of truth for drive/disc state and operations."""
    
    def __init__(self, db: Session):
        self.db = db
    
    def can_start_rip(
        self,
        disc_hash: str,
        disc_num: str,
        mount_point: str,
        rip_request_id: Optional[str] = None
    ) -> Tuple[bool, Optional[db_models.Job]]:
        """
        Check if a rip can start for the given disc.

        This is the API's single authority for rip duplicate prevention: the API calls
        can_start_rip before start_rip; only when can_start is True does it queue the
        rip_disc task. The worker does not perform duplicate checks.
        
        Returns:
            Tuple of (can_start: bool, existing_job: Optional[Job])
            - If can_start is False, existing_job contains the blocking job
            - If can_start is True, existing_job is None
        """
        # If disc_hash is empty or a temporary pending hash, check by mount_point instead
        use_mount_point_check = not disc_hash or disc_hash.startswith("pending-")

        logger.info(
            "can_start_rip START rid=%s disc_num=%s mount_point=%s disc_hash=%s use_mount_point=%s",
            rip_request_id, disc_num, mount_point, disc_hash, use_mount_point_check
        )

        # Use SELECT FOR UPDATE FIRST to lock rows and prevent race conditions
        # Then use unified pipeline inside the transaction for accurate checking
        try:
            # Exclude failed jobs so they never block starting a new rip
            _not_failed = ~or_(
                db_models.Job.job_status == "failed",
                db_models.Job.rip_state == "failed",
            )
            if use_mount_point_check:
                # Check by mount_point if disc_hash is not available
                existing = (
                    self.db.query(db_models.Job)
                    .filter(
                        db_models.Job.mount_point == mount_point,
                        or_(
                            db_models.Job.job_status.in_(["pending", "running", "validating"]),
                            db_models.Job.rip_state.in_(["pending", "running"]),
                            db_models.Job.transfer_state.in_(["pending", "running"]),
                        ),
                        _not_failed,
                    )
                    .with_for_update(nowait=True)  # Lock rows to prevent concurrent creation
                    .order_by(db_models.Job.created_at.desc())
                    .first()
                )
            else:
                # Check by disc_hash (normal case)
                existing = (
                    self.db.query(db_models.Job)
                    .join(db_models.Disc, db_models.Job.disc_id == db_models.Disc.id)
                    .filter(
                        db_models.Disc.content_hash == disc_hash,
                        or_(
                            db_models.Job.job_status.in_(["pending", "running", "validating"]),
                            db_models.Job.rip_state.in_(["pending", "running"]),
                            db_models.Job.transfer_state.in_(["pending", "running"]),
                        ),
                        _not_failed,
                    )
                    .with_for_update(nowait=True)  # Lock rows to prevent concurrent creation
                    .order_by(db_models.Job.created_at.desc())
                    .first()
                )
            
            logger.debug(
                "can_start_rip rid=%s SELECT FOR UPDATE found existing=%s (id=%s status=%s rip_state=%s)",
                rip_request_id, existing is not None,
                existing.id if existing else None,
                existing.job_status if existing else None,
                getattr(existing, "rip_state", None) if existing else None
            )
            
            # Now use unified pipeline INSIDE the transaction to check if rip is actually running
            # This reduces the race window and ensures we see the locked state
            is_running, active_job = is_rip_running_for_disc(self.db, disc_hash, disc_num, mount_point)
            
            logger.debug(
                "can_start_rip rid=%s unified pipeline returned is_running=%s active_job=%s (id=%s)",
                rip_request_id, is_running, active_job is not None,
                active_job.id if active_job else None
            )
            
            # If we found an existing job via SELECT FOR UPDATE, check if it's actually running
            # Only treat it as running if it's in running state, not just pending
            if existing:
                existing_job_status = existing.job_status
                existing_rip_state = getattr(existing, "rip_state", None)
                existing_is_running = existing_job_status == "running" or existing_rip_state == "running"
                
                logger.debug(
                    "can_start_rip rid=%s existing job analysis: job_status=%s rip_state=%s existing_is_running=%s",
                    rip_request_id, existing_job_status, existing_rip_state, existing_is_running
                )
                
                # If unified pipeline didn't find an active job, but we have an existing job via SELECT FOR UPDATE,
                # use the existing job only if it's actually in running state (not just pending)
                if not active_job and existing_is_running:
                    logger.debug(
                        "can_start_rip rid=%s using existing job (unified pipeline found nothing, but existing is running)",
                        rip_request_id
                    )
                    active_job = existing
                    is_running = True
                elif active_job and str(existing.id) == str(active_job.id):
                    # Same job found by both - use the existing one for consistency
                    logger.debug(
                        "can_start_rip rid=%s using existing job (same job found by both SELECT FOR UPDATE and unified pipeline)",
                        rip_request_id
                    )
                    active_job = existing
                    is_running = True
                elif existing_is_running:
                    # Existing job is in running state but unified pipeline didn't find it
                    # This could be a race condition - use the existing job
                    logger.debug(
                        "can_start_rip rid=%s using existing job (existing is running but unified pipeline didn't find it - race condition?)",
                        rip_request_id
                    )
                    active_job = existing
                    is_running = True
                else:
                    logger.debug(
                        "can_start_rip rid=%s existing job is not running (status=%s rip_state=%s), not using it to block",
                        rip_request_id, existing_job_status, existing_rip_state
                    )
            
            if is_running:
                if active_job:
                    # Refresh to get latest state
                    try:
                        self.db.refresh(active_job)
                    except Exception:
                        pass
                    
                    rip_state = getattr(active_job, "rip_state", None)
                    job_status = active_job.job_status
                    celery_task_id = getattr(active_job, "celery_task_id", None)
                    rip_pid = getattr(active_job, "rip_pid", None)
                    
                    # Check if this is a stale job (DB says running but process check says not)
                    is_active = is_rip_task_active(active_job)
                    if job_status == "running" or rip_state == "running":
                        if not is_active:
                            # Stale job detected - reconcile immediately
                            logger.warning(
                                "can_start_rip detected stale job via unified pipeline: DB says running but process check says not running. "
                                "Marking job as failed and allowing new rip. rid=%s disc_num=%s mount_point=%s disc_hash=%s "
                                "existing_job=%s job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s",
                                rip_request_id, disc_num, mount_point, disc_hash,
                                active_job.id, job_status, rip_state, celery_task_id, rip_pid
                            )
                            # Immediately reconcile the state
                            try:
                                error_msg = "Job failed: stale job detected (process not running but DB says running)"
                                updates = {
                                    "job_status": "failed",
                                    "rip_state": "failed" if rip_state not in ("completed",) else rip_state,
                                    "error_reason": error_msg,
                                }
                                apply_job_state(
                                    self.db,
                                    active_job,
                                    updates=updates,
                                    reason="stale job detected (process not running but DB says running)"
                                )
                                logger.info(
                                    "Successfully reconciled stale job %s - allowing new rip to proceed rid=%s",
                                    active_job.id, rip_request_id
                                )
                            except Exception as reconcile_exc:
                                logger.error(
                                    "Failed to reconcile stale job %s: %s. Blocking new rip to be safe. rid=%s",
                                    active_job.id, reconcile_exc, rip_request_id,
                                    exc_info=True
                                )
                                self.db.rollback()
                                # If reconciliation fails, block to be safe
                                return False, active_job
                            
                            # Now allow new rip to proceed
                            return True, None
                    
                    # Rip is actually running - block
                    logger.warning(
                        "can_start_rip blocking rip (unified pipeline) rid=%s disc_num=%s mount_point=%s disc_hash=%s "
                        "existing_job=%s job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s",
                        rip_request_id, disc_num, mount_point, disc_hash,
                        active_job.id, job_status, rip_state, celery_task_id, rip_pid
                    )
                    return False, active_job
                else:
                    # Process is running but no job found - this is an orphaned process
                    # Allow new rip to proceed (the orphaned process will be killed when the new job starts)
                    logger.warning(
                        "can_start_rip rid=%s: unified pipeline detected orphaned process (no job found). "
                        "Allowing new rip to proceed - orphaned process will be handled by new job. "
                        "disc_num=%s mount_point=%s disc_hash=%s",
                        rip_request_id, disc_num, mount_point, disc_hash
                    )
                    return True, None
            
            # Unified pipeline says no rip is running - check if we found an existing job via SELECT FOR UPDATE
            logger.debug(
                "can_start_rip rid=%s unified pipeline says no rip running, checking existing job from SELECT FOR UPDATE",
                rip_request_id
            )
            if existing:
                # Refresh to get latest state
                try:
                    self.db.refresh(existing)
                except Exception:
                    pass
                
                # Double-check with unified pipeline (in case something changed between checks)
                is_running_check, active_job_check = is_rip_running_for_disc(self.db, disc_hash, disc_num, mount_point)
                if is_running_check and active_job_check and active_job_check.id == existing.id:
                    # Job is actually active - block
                    rip_state = getattr(existing, "rip_state", None)
                    job_status = existing.job_status
                    celery_task_id = getattr(existing, "celery_task_id", None)
                    rip_pid = getattr(existing, "rip_pid", None)
                    logger.warning(
                        "can_start_rip blocking rip (SELECT FOR UPDATE + unified pipeline) rid=%s disc_num=%s mount_point=%s disc_hash=%s "
                        "existing_job=%s job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s",
                        rip_request_id, disc_num, mount_point, disc_hash,
                        existing.id, job_status, rip_state, celery_task_id, rip_pid
                    )
                    return False, existing
                
                # Job found by SELECT FOR UPDATE but unified pipeline says not active
                # This could be a race condition - check if it's a stale job
                rip_state = getattr(existing, "rip_state", None)
                job_status = existing.job_status
                is_rip_running = rip_state == "running" or job_status == "running"
                
                if is_rip_running or job_status == "pending":
                    # Verify with unified pipeline check
                    is_active = is_rip_task_active(existing)
                    if is_rip_running and not is_active:
                        # Stale job - reconcile
                        logger.warning(
                            "can_start_rip detected stale job (SELECT FOR UPDATE) rid=%s disc_num=%s mount_point=%s disc_hash=%s "
                            "existing_job=%s job_status=%s rip_state=%s",
                            rip_request_id, disc_num, mount_point, disc_hash,
                            existing.id, job_status, rip_state
                        )
                        try:
                            error_msg = "Job failed: stale job detected (process not running but DB says running)"
                            updates = {
                                "job_status": "failed",
                                "rip_state": "failed" if rip_state not in ("completed",) else rip_state,
                                "error_reason": error_msg,
                            }
                            apply_job_state(
                                self.db,
                                existing,
                                updates=updates,
                                reason="stale job detected (process not running but DB says running)"
                            )
                            logger.info(
                                "Successfully reconciled stale job %s - allowing new rip to proceed rid=%s",
                                existing.id, rip_request_id
                            )
                        except Exception as reconcile_exc:
                            logger.error(
                                "Failed to reconcile stale job %s: %s. Blocking new rip to be safe. rid=%s",
                                existing.id, reconcile_exc, rip_request_id,
                                exc_info=True
                            )
                            self.db.rollback()
                            return False, existing
                        return True, None
                    if job_status == "pending" and not is_active:
                        # Pending job but no task/process running - allow new rip so Start Copy actually starts makemkvcon
                        logger.info(
                            "can_start_rip existing job %s is pending but not active (no task running) - allowing new rip rid=%s disc_num=%s mount_point=%s",
                            existing.id, rip_request_id, disc_num, mount_point
                        )
                        try:
                            error_msg = "Job superseded: previous attempt never started or did not run; starting new rip."
                            updates = {
                                "job_status": "failed",
                                "rip_state": "failed" if rip_state not in ("completed",) else rip_state,
                                "error_reason": error_msg,
                            }
                            apply_job_state(
                                self.db,
                                existing,
                                updates=updates,
                                reason="pending job not active - allowing new rip",
                            )
                            logger.info(
                                "Marked pending job %s as failed so new rip can proceed rid=%s",
                                existing.id, rip_request_id
                            )
                        except Exception as reconcile_exc:
                            logger.error(
                                "Failed to mark pending job %s: %s. Allowing new rip anyway. rid=%s",
                                existing.id, reconcile_exc, rip_request_id,
                                exc_info=True
                            )
                            self.db.rollback()
                        return True, None
                
                # Job exists but rip is not running (e.g., in post-processing)
                # Defensive: do not block on failed jobs - allow new rip
                if job_status == "failed" or rip_state == "failed":
                    logger.info(
                        "can_start_rip rid=%s existing job %s is failed - allowing new rip (not blocking)",
                        rip_request_id, existing.id,
                    )
                    return True, None
                logger.info(
                    "Active job exists for disc %s (hash=%s id=%s status=%s rip_state=%s); returning existing jobId",
                    disc_num, disc_hash, existing.id, job_status, rip_state
                )
                return False, existing

            if not use_mount_point_check:
                # If disc_hash check found nothing, fall back to mount_point check to avoid hash mismatch bypass.
                # Only block if the rip is actually still running, not just if job is in certain states.
                fallback_existing = (
                    self.db.query(db_models.Job)
                    .filter(
                        db_models.Job.mount_point == mount_point,
                        or_(
                            db_models.Job.job_status.in_(["pending", "running", "validating"]),
                            db_models.Job.rip_state.in_(["pending", "running"]),
                            db_models.Job.transfer_state.in_(["pending", "running"]),
                        ),
                        _not_failed,
                    )
                    .order_by(db_models.Job.created_at.desc())
                    .first()
                )
                if fallback_existing:
                    # Check if the rip is actually still running
                    # If rip_state=completed, the rip is done and we should allow a new rip
                    rip_state = getattr(fallback_existing, "rip_state", None)
                    job_status = fallback_existing.job_status
                    
                    # If rip is completed, don't block even if job is validating
                    if rip_state == "completed":
                        logger.info(
                            "can_start_rip fallback mount_point found job with completed rip_state - allowing new rip "
                            "rid=%s disc_num=%s mount_point=%s job_id=%s status=%s rip_state=%s",
                            rip_request_id,
                            disc_num,
                            mount_point,
                            fallback_existing.id,
                            job_status,
                            rip_state,
                        )
                        # Don't block - rip is done, validation can happen in parallel
                    else:
                        # Verify the job is actually active using unified pipeline check
                        is_active = is_rip_task_active(fallback_existing)
                        if is_active:
                            logger.warning(
                                "can_start_rip fallback mount_point hit rid=%s disc_num=%s mount_point=%s job_id=%s status=%s rip_state=%s",
                                rip_request_id,
                                disc_num,
                                mount_point,
                                fallback_existing.id,
                                job_status,
                                rip_state,
                            )
                            return False, fallback_existing
                        else:
                            # Job found but not actually active - might be stale
                            logger.info(
                                "can_start_rip fallback mount_point found job but it's not active - allowing new rip "
                                "rid=%s disc_num=%s mount_point=%s job_id=%s status=%s rip_state=%s",
                                rip_request_id,
                                disc_num,
                                mount_point,
                                fallback_existing.id,
                                job_status,
                                rip_state,
                            )
                            # Don't block - job is not actually active
            
            logger.info(
                "can_start_rip rid=%s RETURNING True, None (no rip running, can start)",
                rip_request_id
            )
            return True, None
            
        except OperationalError as op_exc:
            # Lock timeout - another transaction has the row locked
            # This means another request is likely creating a job, so check again without the lock
            self.db.rollback()
            logger.warning(
                "can_start_rip rid=%s lock timeout (OperationalError): %s. Checking for existing job without lock.",
                rip_request_id, op_exc
            )
            if use_mount_point_check:
                # Check by mount_point (exclude failed jobs)
                _not_failed_retry = ~or_(
                    db_models.Job.job_status == "failed",
                    db_models.Job.rip_state == "failed",
                )
                existing = (
                    self.db.query(db_models.Job)
                    .filter(
                        db_models.Job.mount_point == mount_point,
                        or_(
                            db_models.Job.job_status.in_(["pending", "running", "validating"]),
                            db_models.Job.rip_state.in_(["pending", "running"]),
                        ),
                        _not_failed_retry,
                    )
                    .order_by(db_models.Job.created_at.desc())
                    .first()
                )
            else:
                existing = crud.get_active_job_for_hash(self.db, disc_hash)
            if existing:
                logger.info(
                    "Lock timeout detected, found existing job %s (likely created by concurrent request) rid=%s",
                    existing.id, rip_request_id
                )
                return False, existing
            else:
                logger.warning(
                    "can_start_rip rid=%s lock timeout but no existing job found - may be a race condition. Allowing request to proceed.",
                    rip_request_id
                )
                # Allow the request to proceed - worst case we'll catch it in start_rip
                return True, None
        except Exception as exc:
            logger.error(
                "can_start_rip rid=%s EXCEPTION: Error checking if rip can start: %s",
                rip_request_id, exc, exc_info=True
            )
            self.db.rollback()
            logger.error(
                "can_start_rip rid=%s RETURNING False, None due to exception. This will cause 409 error. "
                "disc_num=%s mount_point=%s disc_hash=%s",
                rip_request_id, disc_num, mount_point, disc_hash
            )
            return False, None
    
    def start_rip(
        self,
        disc_hash: str,
        disc_num: str,
        mount_point: str,
        mode: str = "copy",
        output_dir: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        rip_request_id: Optional[str] = None
    ) -> db_models.Job:
        """
        Start a rip operation. This is the ONLY way to initiate a rip.

        Duplicate prevention: callers must call can_start_rip first; start_rip also
        calls can_start_rip and returns the existing job if a rip is already running.
        Only when can_start is True does this method create a job and queue the
        rip_disc task.
        
        Creates job in DB, dispatches Celery task, and stores celery_task_id.
        
        Returns:
            The created Job object
        """
        # Double-check we can start (in case of race condition)
        logger.info(
            "start_rip request rid=%s disc_num=%s mount_point=%s disc_hash=%s mode=%s output_dir=%s",
            rip_request_id, disc_num, mount_point, disc_hash, mode, output_dir
        )
        can_start, existing_job = self.can_start_rip(disc_hash, disc_num, mount_point, rip_request_id=rip_request_id)
        if not can_start:
            if existing_job:
                logger.debug("start_rip rid=%s returning existing job=%s", rip_request_id, existing_job.id)
                # If existing job is pending and task is not really running, re-dispatch (PENDING = inactive after restart)
                existing_status = getattr(existing_job, "job_status", None)
                if existing_status == "pending" and not is_rip_task_really_running(existing_job):
                    try:
                        # Set rip_state=running before dispatch so eager task's rip-complete callback succeeds.
                        try:
                            StageState.rip_started(self.db, existing_job, reason="rip_redispatch")
                            self.db.commit()
                        except Exception as state_exc:
                            logger.warning("Optimistic state update before redispatch failed: %s", state_exc)
                            self.db.rollback()
                        out_dir = getattr(existing_job, "output_dir", None) or output_dir
                        task_result = rip_disc.apply_async(
                            args=(str(existing_job.id), disc_num, mount_point, mode, out_dir),
                            kwargs={"rip_request_id": rip_request_id},
                            task_id=f"rip_disc:{existing_job.id}",
                        )
                        existing_job.celery_task_id = task_result.id
                        self.db.commit()
                        logger.info(
                            "gatekeeper re-dispatched rip_disc for pending job=%s (return_existing path) task_id=%s",
                            existing_job.id, task_result.id,
                        )
                    except Exception as dispatch_exc:
                        logger.warning(
                            "gatekeeper failed to re-dispatch for pending job=%s (return_existing path): %s",
                            existing_job.id, dispatch_exc,
                        )
                logger.warning(
                    "start_rip blocked rid=%s existing_job=%s status=%s rip_state=%s",
                    rip_request_id,
                    existing_job.id,
                    getattr(existing_job, "job_status", None),
                    getattr(existing_job, "rip_state", None),
                )
                return existing_job
            raise ValueError("Cannot start rip: duplicate check failed")
        
        # Get disc info if not provided
        if payload is None:
            try:
                # Use get_cached_discs to find disc info (no direct drive operations)
                # This is safe because disc should already be scanned and cached before rip starts
                cached_discs = get_cached_discs()
                payload = None
                for disc in cached_discs:
                    if disc.get("disc_num") == str(disc_num) and disc.get("mount_point") == mount_point:
                        payload = disc
                        break
                
                if not payload:
                    # Try using self.get_disc_info which checks DB cache first
                    try:
                        payload = self.get_disc_info(disc_hash, disc_num, mount_point, refresh=False)
                    except Exception:
                        # If that also fails, raise the original error
                        raise ValueError(f"Disc info not found in cache for disc_num={disc_num} mount_point={mount_point}. Disc may need to be scanned first.")
            except Exception as exc:
                logger.error("Failed to get disc info for rip: %s", exc)
                raise ValueError(f"Failed to load disc info: {exc}") from exc
        
        # Get hash from payload
        payload_hash = payload.get("disc_hash") or payload.get("content_hash")
        if not payload_hash:
            raise ValueError("disc_hash missing from payload")
        
        # If disc_hash was not provided (empty string), use the hash from payload
        # Otherwise, validate that they match
        if disc_hash:
            if str(payload_hash) != str(disc_hash):
                raise ValueError(f"Disc hash mismatch (expected {disc_hash}, got {payload_hash})")
        else:
            # Use the hash from payload
            disc_hash = payload_hash
            logger.info("Using computed disc_hash from payload: %s", disc_hash)
        
        # Early check: Check if a job already exists with the celery_task_id that would be generated
        # This prevents creating duplicate jobs that would fail later due to unique constraint violation
        # However, if the existing job is failed, we should allow creating a new job for retry
        prospective_task_id = f"rip_disc:{disc_hash}"
        existing_job_with_task_id = crud.get_job_by_task_id(self.db, prospective_task_id)
        if existing_job_with_task_id:
            # Check if the existing job is in a failed state
            job_status = getattr(existing_job_with_task_id, "job_status", None)
            rip_state = getattr(existing_job_with_task_id, "rip_state", None)
            is_failed = job_status == "failed" or rip_state == "failed"
            
            # CRITICAL: Check if rip is actually running (PENDING = not running, so we can re-dispatch after restart)
            is_active = is_rip_task_really_running(existing_job_with_task_id)
            
            if is_active:
                logger.warning(
                    "Job already exists with celery_task_id %s for disc %s (hash=%s) and rip is ACTIVE (Celery+PID); "
                    "blocking new job creation and returning existing job %s (job_status=%s, rip_state=%s, rip_pid=%s)",
                    prospective_task_id, disc_num, disc_hash, existing_job_with_task_id.id, 
                    job_status, rip_state, getattr(existing_job_with_task_id, "rip_pid", None)
                )
                return existing_job_with_task_id
            
            if is_failed:
                # Job is failed and not active - allow creating a new job for retry
                logger.info(
                    "Job already exists with celery_task_id %s for disc %s (hash=%s) but is in failed state and not active "
                    "(job_status=%s, rip_state=%s). Allowing new job creation for retry instead of returning failed job %s",
                    prospective_task_id, disc_num, disc_hash, job_status, rip_state, existing_job_with_task_id.id
                )
                # Don't return the failed job - allow creating a new one
                # Note: This will cause a unique constraint violation on celery_task_id, which we handle below
            else:
                logger.debug(
                    "start_rip rid=%s returning existing job=%s (task_id=%s)",
                    rip_request_id, existing_job_with_task_id.id, prospective_task_id,
                )
                # If existing job is pending, re-dispatch with per-job task_id (fresh message after restart)
                if job_status == "pending":
                    try:
                        # Set rip_state=running before dispatch so eager task's rip-complete callback succeeds.
                        try:
                            StageState.rip_started(self.db, existing_job_with_task_id, reason="rip_redispatch")
                            self.db.commit()
                        except Exception as state_exc:
                            logger.warning("Optimistic state update before redispatch failed: %s", state_exc)
                            self.db.rollback()
                        out_dir = getattr(existing_job_with_task_id, "output_dir", None) or output_dir
                        task_result = rip_disc.apply_async(
                            args=(str(existing_job_with_task_id.id), disc_num, mount_point, mode, out_dir),
                            kwargs={"rip_request_id": rip_request_id},
                            task_id=f"rip_disc:{existing_job_with_task_id.id}",
                        )
                        existing_job_with_task_id.celery_task_id = task_result.id
                        self.db.commit()
                        logger.info(
                            "gatekeeper re-dispatched rip_disc for pending job=%s task_id=%s",
                            existing_job_with_task_id.id, task_result.id,
                        )
                    except Exception as dispatch_exc:
                        logger.warning(
                            "gatekeeper failed to re-dispatch for pending job=%s: %s",
                            existing_job_with_task_id.id, dispatch_exc,
                        )
                logger.info(
                    "Job already exists with celery_task_id %s for disc %s (hash=%s). Returning existing job %s",
                    prospective_task_id, disc_num, disc_hash, existing_job_with_task_id.id
                )
                return existing_job_with_task_id
        
        # Create job
        try:
            job = crud.create_job(
                self.db,
                disc_num,
                mount_point,
                mode,
                output_dir=output_dir,
                payload=payload,
            )
            self.db.flush()  # Flush to make job visible within this transaction
            
            # Double-check after flush to catch race conditions
            existing = crud.get_active_job_for_hash(self.db, disc_hash) if disc_hash else None
            if existing and str(existing.id) != str(job.id):
                # Another job was created concurrently - rollback and return the existing one
                self.db.rollback()
                logger.info(
                    "Duplicate job creation detected (race condition prevented) rid=%s disc=%s hash=%s returning existing jobId=%s instead of %s",
                    rip_request_id, disc_num, disc_hash, existing.id, job.id
                )
                return existing
            
            # Commit transaction BEFORE dispatching task
            self.db.commit()
            
            # Set rip_state=running (and job_status) BEFORE dispatching so that when the task runs
            # (e.g. with CELERY_TASK_ALWAYS_EAGER) the rip-complete callback sees running state.
            try:
                StageState.rip_started(self.db, job, reason="rip_dispatched")
                self.db.commit()
            except Exception as state_exc:
                logger.warning("Optimistic state update before dispatch failed: %s", state_exc)
                self.db.rollback()
            
            # Dispatch Celery task with stable task_id per job so redispatch reuses same id
            task_id = f"rip_disc:{job.id}"
            try:
                # Log call stack to trace what triggered this dispatch
                import traceback
                call_stack = ''.join(traceback.format_stack()[-5:-1])  # Last 4 frames (excluding this one and apply_async)
                logger.info(
                    "DISPATCHING rip_disc task rid=%s task_id=%s job=%s disc_num=%s mount_point=%s disc_hash=%s mode=%s "
                    "job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s call_stack=%s",
                    rip_request_id, task_id, job.id, disc_num, mount_point, disc_hash, mode,
                    getattr(job, "job_status", None), getattr(job, "rip_state", None),
                    getattr(job, "celery_task_id", None), getattr(job, "rip_pid", None),
                    call_stack.replace('\n', ' | ')
                )
                
                task_result = rip_disc.apply_async(
                    args=(str(job.id), disc_num, mount_point, mode, output_dir),
                    kwargs={"rip_request_id": rip_request_id},
                    task_id=task_id
                )
                logger.info(
                    "Dispatched Celery task rip_disc rid=%s task_id=%s celery_task_id=%s job=%s disc_num=%s mount_point=%s",
                    rip_request_id, task_id, task_result.id, job.id, disc_num, mount_point
                )
                
                # Store celery_task_id (rip_state already set to running before dispatch)
                try:
                    job.celery_task_id = task_result.id
                    self.db.commit()
                except Exception as exc:
                    self.db.rollback()
                    # Check if this is a unique constraint violation
                    from sqlalchemy.exc import IntegrityError
                    if isinstance(exc, IntegrityError) and "uq_jobs_celery_task_id" in str(exc.orig):
                        # Another job already has this celery_task_id - find it and check its state
                        logger.warning(
                            "celery_task_id %s already exists (race condition detected despite early check). Finding existing job.",
                            task_result.id
                        )
                        existing_job = crud.get_job_by_task_id(self.db, task_result.id)
                        if existing_job:
                            # Check if the existing job is failed
                            existing_job_status = getattr(existing_job, "job_status", None)
                            existing_rip_state = getattr(existing_job, "rip_state", None)
                            is_existing_failed = existing_job_status == "failed" or existing_rip_state == "failed"
                            
                            if is_existing_failed:
                                # The existing job is failed - clear its celery_task_id and retry setting it on the new job
                                logger.info(
                                    "Existing job %s with celery_task_id %s is failed (job_status=%s, rip_state=%s). "
                                    "Clearing celery_task_id from failed job and using it for new job %s",
                                    existing_job.id, task_result.id, existing_job_status, existing_rip_state, job.id
                                )
                                existing_job.celery_task_id = None
                                self.db.commit()
                                
                                # Now retry setting celery_task_id on the new job
                                try:
                                    job.celery_task_id = task_result.id
                                    self.db.commit()
                                    logger.info("Successfully set celery_task_id %s on new job %s after clearing it from failed job", task_result.id, job.id)
                                except Exception as retry_exc:
                                    self.db.rollback()
                                    logger.error("Failed to set celery_task_id on new job after clearing from failed job: %s", retry_exc)
                                    raise
                            else:
                                # Existing job is not failed - return it instead of creating duplicate
                                logger.info(
                                    "Found existing job %s with celery_task_id %s. Returning existing job instead of %s (duplicate job will remain in pending state but won't be marked as failed)",
                                    existing_job.id, task_result.id, job.id
                                )
                                # Don't mark the duplicate job as failed - just return the existing job
                                # The duplicate job will remain in its current state (likely 'pending')
                                # If the task was already dispatched, Celery will handle it appropriately
                                return existing_job
                        else:
                            logger.error(
                                "Unique constraint violation but no existing job found for celery_task_id %s. "
                                "This is an edge case - task was dispatched but job lookup failed.",
                                task_result.id
                            )
                            # Mark job as failed only if we truly can't find the existing job (edge case)
                            job.job_status = 'failed'
                            job.error_reason = f"Race condition: celery_task_id {task_result.id} conflict but existing job not found"
                            self.db.commit()
                    else:
                        logger.error("Failed to store celery_task_id %s: %s", task_result.id, exc)
                    # Don't fail the whole operation - task is already queued, but log the issue
                
                return job
                
            except Exception as exc:
                logger.error("Failed to dispatch Celery task: %s", exc)
                # Mark job as failed since we couldn't dispatch the task
                self.db.rollback()
                job = crud.get_job(self.db, job.id)
                if job:
                    job.job_status = 'failed'
                    job.error_reason = f"Failed to dispatch Celery task: {exc}"
                    self.db.commit()
                raise ValueError(f"Failed to dispatch Celery task: {exc}") from exc
                
        except Exception as exc:
            self.db.rollback()
            # Check if job was created by concurrent request before re-raising
            existing = crud.get_active_job_for_hash(self.db, disc_hash) if disc_hash else None
            if existing:
                logger.info(
                    "Job creation failed but existing job found (concurrent creation) for disc %s (hash=%s id=%s); returning existing jobId",
                    disc_num, disc_hash, existing.id
                )
                return existing
            raise
    
    def get_disc_info(self, disc_hash: Optional[str], disc_num: str, mount_point: str, refresh: bool = False) -> Dict[str, Any]:
        """
        Get disc info, using hash-based detection to skip unnecessary scans.
        
        If disc_hash is provided and disc exists in DB with scan_state='completed',
        return cached info without scanning.
        
        If disc exists but scan_state='failed', allow retry if refresh=True.
        """
        # If we have a hash, check DB first
        if disc_hash:
            disc_record = (
                self.db.query(db_models.Disc)
                .filter(db_models.Disc.content_hash == disc_hash)
                .first()
            )
            
            if disc_record:
                # Check scan state
                scan_state = getattr(disc_record, "scan_state", None)
                
                if scan_state == "completed" and not refresh:
                    # Disc already scanned successfully - return cached info
                    logger.info("Disc %s (hash=%s) already scanned, returning cached info", disc_num, disc_hash)
                    # Build payload from disc record
                    payload = {
                        "disc_num": disc_num,
                        "mount_point": mount_point,
                        "disc_hash": disc_hash,
                        "content_hash": disc_hash,
                        "info_title": disc_record.info_title,
                        "disc_slug": disc_record.disc_slug,
                        "disc_name": disc_record.disc_name,
                        "format": disc_record.format,
                    }
                    # Add release info if available
                    if disc_record.release:
                        payload["release_id"] = str(disc_record.release_id)
                        payload["disc_group"] = disc_record.release.slug
                        payload["release_name"] = disc_record.release.name
                    
                    # Get titles from disc_titles
                    if disc_record.titles:
                        titles = {}
                        for title in disc_record.titles:
                            titles[str(title.source_file or title.id)] = {
                                "file": title.source_file,
                                "title": title.title,
                                "description": title.description,
                            }
                        payload["titles"] = titles
                    
                    # Merge disc_info into payload if available
                    if disc_record.disc_info:
                        disc_scan_info = disc_record.disc_info
                        payload.update(disc_scan_info)
                    
                    return payload
                
                elif scan_state == "failed" and not refresh:
                    # Disc scan failed previously - return error info
                    logger.warning(
                        "Disc %s (hash=%s) has failed scan state, last error: %s",
                        disc_num, disc_hash, getattr(disc_record, "last_scan_error", None)
                    )
                    raise DriveManagerError(
                        f"Disc scan previously failed: {getattr(disc_record, 'last_scan_error', 'Unknown error')}. "
                        f"Use recover_failed_scan() to retry.",
                        status_code=409
                    )
        
        # No hash or disc not found or refresh requested - get info from cache
        try:
            # Use get_cached_discs to find disc info (no direct drive operations)
            cached_discs = get_cached_discs()
            payload = None
            for disc in cached_discs:
                if disc.get("disc_num") == str(disc_num) and disc.get("mount_point") == mount_point:
                    payload = disc
                    break
            
            if not payload:
                raise DriveManagerError(
                    f"Disc info not found in cache for disc_num={disc_num} mount_point={mount_point}. "
                    f"Disc may need to be scanned first.",
                    status_code=404
                )
            
            # Update scan state and store disc scan info in DB if we have a hash
            if disc_hash and payload.get("disc_hash"):
                self._update_scan_state(disc_hash, "completed", error=None)
                # Store disc scan info in disc.disc_info
                disc_record = (
                    self.db.query(db_models.Disc)
                    .filter(db_models.Disc.content_hash == disc_hash)
                    .first()
                )
                if disc_record:
                    from api import crud
                    disc_scan_info = crud._extract_disc_scan_info(payload)
                    if disc_scan_info:
                        crud._store_disc_scan_info(self.db, disc_record, disc_scan_info)
            
            return payload
            
        except Exception as exc:
            # Mark scan as failed if we have a hash
            if disc_hash:
                self._update_scan_state(disc_hash, "failed", error=str(exc))
            raise
    
    def recover_failed_scan(self, disc_num: str, mount_point: str, disc_hash: Optional[str] = None) -> Dict[str, Any]:
        """
        Recover from a failed scan by retrying the info scan.
        
        Updates scan_attempts and clears error on success.
        """
        if not disc_hash:
            # Try to get hash from disc record
            disc_record = (
                self.db.query(db_models.Disc)
                .filter(db_models.Disc.disc_num == disc_num)
                .order_by(db_models.Disc.created_at.desc())
                .first()
            )
            if disc_record:
                disc_hash = disc_record.content_hash
            else:
                raise ValueError("disc_hash required for recovery")
        
        # Get current disc record
        disc_record = (
            self.db.query(db_models.Disc)
            .filter(db_models.Disc.content_hash == disc_hash)
            .first()
        )
        
        if disc_record:
            # Increment scan attempts
            scan_attempts = getattr(disc_record, "scan_attempts", 0) or 0
            disc_record.scan_attempts = scan_attempts + 1
            disc_record.scan_state = "scanning"
            disc_record.last_scan_at = datetime.now(timezone.utc)
            self.db.commit()
        
        try:
            # Retry scan - use get_cached_discs (refresh should be handled by disc_manager cache refresh)
            cached_discs = get_cached_discs()
            payload = None
            for disc in cached_discs:
                if disc.get("disc_num") == str(disc_num) and disc.get("mount_point") == mount_point:
                    payload = disc
                    break
            
            if not payload:
                raise DriveManagerError(
                    f"Disc info not found in cache for disc_num={disc_num} mount_point={mount_point} after recovery attempt. "
                    f"Disc may need to be rescanned.",
                    status_code=404
                )
            
            # Update scan state to completed
            self._update_scan_state(disc_hash, "completed", error=None)
            
            logger.info("Successfully recovered from failed scan for disc %s (hash=%s)", disc_num, disc_hash)
            return payload
            
        except Exception as exc:
            # Mark scan as failed again
            self._update_scan_state(disc_hash, "failed", error=str(exc))
            raise
    
    def update_rip_state(
        self,
        job_id: str,
        state: str,
        progress: Optional[int] = None,
        error_reason: Optional[str] = None,
        **kwargs
    ) -> None:
        """
        Update rip state. This is the ONLY method that should modify rip state.
        
        Args:
            job_id: Job ID
            state: New rip state ('pending', 'running', 'completed', 'failed')
            progress: Optional progress percentage (0-100)
            error_reason: Optional error message if state is 'failed'
            **kwargs: Additional fields to update (e.g., rip_progress, titles_completed)
        """
        job = crud.get_job(self.db, job_id)
        if not job:
            logger.warning("Cannot update rip state: job %s not found", job_id)
            return
        
        previous_rip_state = getattr(job, "rip_state", None)
        previous_job_status = getattr(job, "job_status", None)
        logger.info(
            "update_rip_state job=%s rip_state=%s->%s job_status=%s->%s progress=%s reason=%s",
            job_id,
            previous_rip_state,
            state,
            previous_job_status,
            ("failed" if state == "failed" else previous_job_status),
            progress,
            error_reason,
        )
        # Update rip_state
        job.rip_state = state
        
        # Update progress if provided
        if progress is not None:
            job.rip_progress = progress
        
        # Update error reason if provided
        if error_reason:
            job.error_reason = error_reason
        
        # Update any additional fields
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        
        # Update job_status based on rip_state
        if state == "running":
            job.job_status = "running"
        elif state == "completed":
            # Don't automatically set job_status to completed - other stages may still be pending
            pass
        elif state == "failed":
            job.job_status = "failed"
        
        self.db.commit()
        logger.info("Updated rip state for job %s: %s (progress=%s)", job_id, state, progress)
    
    def get_drive_state(self, disc_num: str, mount_point: str) -> Dict[str, Any]:
        """
        Get current state of a drive from DB.
        
        Returns dict with active operations for this drive.
        """
        # Query for active jobs on this disc
        active_jobs = (
            self.db.query(db_models.Job)
            .join(db_models.Disc, db_models.Job.disc_id == db_models.Disc.id)
            .filter(
                db_models.Job.disc_num == disc_num,
                or_(
                    db_models.Job.job_status.in_(["pending", "running"]),
                    db_models.Job.rip_state.in_(["pending", "running"]),
                ),
            )
            .all()
        )
        
        return {
            "disc_num": disc_num,
            "mount_point": mount_point,
            "active_jobs": [
                {
                    "job_id": str(job.id),
                    "job_status": job.job_status,
                    "rip_state": getattr(job, "rip_state", None),
                    "created_at": job.created_at.isoformat() if job.created_at else None,
                }
                for job in active_jobs
            ],
            "is_busy": len(active_jobs) > 0,
        }
    
    def _update_scan_state(self, disc_hash: str, state: str, error: Optional[str] = None) -> None:
        """Internal method to update scan state in DB."""
        disc = (
            self.db.query(db_models.Disc)
            .filter(db_models.Disc.content_hash == disc_hash)
            .first()
        )
        
        if disc:
            disc.scan_state = state
            if error:
                disc.last_scan_error = error
            else:
                disc.last_scan_error = None
            disc.last_scan_at = datetime.now(timezone.utc)
            
            # Mark info_log_stored if state is completed
            if state == "completed":
                disc.info_log_stored = True
            
            self.db.commit()
        else:
            logger.warning("Cannot update scan state: disc with hash %s not found", disc_hash)

