"""
Failure recovery system that attempts automatic recovery before marking jobs as failed.
Recovers from common failure scenarios based on error reasons and job state.
"""
import logging
from typing import Optional, Tuple, Dict, Any
from sqlalchemy.orm import Session
from datetime import datetime, timedelta

from api import crud, models as db_models
from core.job_state import apply_job_state, apply_job_state_devmode, StateViolation, StageState
from core.utils import is_dev_mode
from workers.tasks import generate_previews, start_transfer
from core.preview_recovery import build_preview_regeneration_state

log = logging.getLogger(__name__)

# Maximum number of recovery attempts per job
MAX_RECOVERY_ATTEMPTS = 3
# Recovery attempt tracking (job_id -> attempt_count)
_recovery_attempts: Dict[str, int] = {}


def should_attempt_recovery(job, error_reason: str) -> bool:
    """
    Determine if a recovery should be attempted based on error reason and job state.
    
    Returns:
        True if recovery should be attempted, False otherwise
    """
    if not error_reason:
        return False
    
    error_lower = error_reason.lower()
    job_id = str(job.id)
    
    # Check recovery attempt limit
    attempts = _recovery_attempts.get(job_id, 0)
    if attempts >= MAX_RECOVERY_ATTEMPTS:
        log.warning(f"Job {job_id}: Max recovery attempts ({MAX_RECOVERY_ATTEMPTS}) reached, not attempting recovery")
        return False
    
    # Recoverable error patterns
    recoverable_patterns = [
        "stuck",
        "timeout",
        "lock held",
        "no updates",
        "validation failed",
        "missing output",
        "hash mismatch",
        "post-process",
        "postprocess",
        "preview",
        "state violation",
        "backward.*transition",
    ]
    
    for pattern in recoverable_patterns:
        if pattern in error_lower:
            return True
    
    return False


def get_recovery_strategy(job, error_reason: str) -> Optional[str]:
    """
    Determine the appropriate recovery strategy based on error reason and job state.
    
    Returns:
        Recovery strategy name or None if no recovery available
    """
    error_lower = error_reason.lower()
    # #365 — derived, not column.
    post_state = job.derived_post_state or ""
    post_state_lower = post_state.lower()
    rip_state = getattr(job, "rip_state", None) or ""
    rip_state_lower = rip_state.lower()
    
    # Post-processing recovery
    if ("post-process" in error_lower or "postprocess" in error_lower or 
        "post_state" in error_lower or "stuck" in error_lower) and post_state_lower == "running":
        if rip_state_lower in ("completed", "skipped"):
            return "resume_postprocess"
    
    # Preview generation recovery
    if "preview" in error_lower and rip_state_lower in ("completed", "skipped"):
        disc_payload = job.disc_payload or {}
        previews = disc_payload.get("previews", {})
        if isinstance(previews, dict):
            preview_status = previews.get("status", "")
            if preview_status in ("queued", "running", "failed"):
                return "regenerate_previews"
    
    # Validation failure recovery
    if "validation" in error_lower or "hash mismatch" in error_lower or "missing output" in error_lower:
        if post_state_lower == "running" and rip_state_lower in ("completed", "skipped"):
            return "resume_postprocess"  # Re-run post-processing to fix validation issues
        elif "preview" in error_lower:
            return "regenerate_previews"
    
    # State transition recovery
    if "state violation" in error_lower or "backward.*transition" in error_lower:
        if post_state_lower == "running":
            return "reset_postprocess_state"
    
    # Lock/timeout recovery
    if "lock" in error_lower or "timeout" in error_lower:
        if post_state_lower == "running" and rip_state_lower in ("completed", "skipped"):
            return "resume_postprocess"  # Retry after lock/timeout
    
    return None


def attempt_recovery(job, db: Session, error_reason: str) -> Tuple[bool, Optional[str]]:
    """
    Attempt to recover from a failure before marking the job as failed.
    
    Args:
        job: Job instance
        db: Database session
        error_reason: The error reason that triggered the failure
        
    Returns:
        Tuple of (recovery_successful, recovery_message)
        If recovery_successful is True, the job should not be marked as failed.
    """
    job_id = str(job.id)
    
    # Check if recovery should be attempted
    if not should_attempt_recovery(job, error_reason):
        return False, None
    
    # Get recovery strategy
    strategy = get_recovery_strategy(job, error_reason)
    if not strategy:
        log.info(f"Job {job_id}: No recovery strategy available for error: {error_reason}")
        return False, None
    
    # Increment recovery attempt counter
    _recovery_attempts[job_id] = _recovery_attempts.get(job_id, 0) + 1
    attempts = _recovery_attempts[job_id]
    
    log.info(f"Job {job_id}: Attempting recovery (strategy={strategy}, attempt={attempts}/{MAX_RECOVERY_ATTEMPTS})")
    
    try:
        if strategy == "resume_postprocess":
            return _recover_postprocess(job, db, error_reason)
        elif strategy == "regenerate_previews":
            return _recover_previews(job, db, error_reason)
        elif strategy == "reset_postprocess_state":
            return _recover_postprocess_state(job, db, error_reason)
        else:
            log.warning(f"Job {job_id}: Unknown recovery strategy: {strategy}")
            return False, None
    except Exception as exc:
        log.error(f"Job {job_id}: Recovery attempt failed: {exc}", exc_info=True)
        return False, f"Recovery failed: {exc}"


def _recover_postprocess(job, db: Session, error_reason: str) -> Tuple[bool, Optional[str]]:
    """Recover from post-processing failure by resuming post-process."""
    job_id = str(job.id)
    
    try:
        # Reset job_status to pending to allow retry. #365 step 5 dropped
        # the post_state column; post-process state is now derived via
        # Job.derived_post_state, so we only flip the underlying
        # job_status here. start_transfer below re-sets transfer_phase
        # ="preparing" via StageState.postprocess_started.
        if is_dev_mode():
            pass
        else:
            try:
                apply_job_state(
                    db,
                    job,
                    updates={
                        "job_status": "pending",
                        "error_reason": f"Recovery attempt: {error_reason}",
                    },
                    reason="failure recovery: reset post-process",
                )
            except StateViolation:
                apply_job_state_devmode(
                    db,
                    job,
                    updates={
                        "job_status": "pending",
                        "error_reason": f"Recovery attempt: {error_reason}",
                    },
                    reason="failure recovery: reset post-process (devmode)",
                )
        
        # Enqueue start_transfer (#365): the unified Phase 2 worker. The
        # legacy resume_postprocess task name still works as a forwarding
        # shim, but new sites target start_transfer directly so the
        # transfer_phase=preparing marker is set without going through the
        # shim's extra wrapping.
        task_result = start_transfer.delay(job_id)
        try:
            StageState.postprocess_started(db, job, reason="failure recovery: re-enqueued start_transfer")
        except Exception as state_exc:
            log.warning("Job %s: postprocess_started after recovery enqueue failed: %s", job_id, state_exc)
        log.info(f"Job {job_id}: Recovery: Re-enqueued start_transfer (task_id={task_result.id if task_result else 'unknown'})")

        return True, f"Recovery: Re-enqueued post-processing (task_id={task_result.id if task_result else 'unknown'})"
    except Exception as exc:
        log.error(f"Job {job_id}: Post-process recovery failed: {exc}", exc_info=True)
        return False, f"Recovery failed: {exc}"


def _recover_previews(job, db: Session, error_reason: str) -> Tuple[bool, Optional[str]]:
    """Recover from preview generation failure by regenerating only missing previews."""
    job_id = str(job.id)

    try:
        disc_payload = job.disc_payload or {}
        post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
        ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
        if not post_paths and not ripped_files:
            existing_tracks = (disc_payload.get("previews") or {}).get("tracks") if isinstance(disc_payload.get("previews"), dict) else {}
            if isinstance(existing_tracks, dict) and existing_tracks:
                tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(
                    job, db, file_paths_override={str(k): None for k in existing_tracks.keys()}
                )
            else:
                log.warning(f"Job {job_id}: Cannot recover previews - no post_paths or ripped_files found")
                return False, "Cannot recover: no post_paths or ripped_files found"
        else:
            tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(job, db)

        if tracks_to_regen:
            overall_status = "running"
        previews = {
            "status": overall_status,
            "tracks": tracks_state,
            "updated_at": datetime.utcnow().isoformat(),
        }
        disc_payload["previews"] = previews

        apply_job_state(
            db,
            job,
            updates={"disc_payload": disc_payload},
            reason="failure recovery: reset previews",
        )

        if tracks_to_regen:
            task_result = generate_previews.delay(job_id, tracks_to_regen)
            log.info(
                f"Job {job_id}: Recovery: Re-enqueued generate_previews for {len(tracks_to_regen)} track(s) "
                f"(task_id={task_result.id if task_result else 'unknown'})"
            )
            return True, f"Recovery: Re-enqueued preview generation (task_id={task_result.id if task_result else 'unknown'})"

        log.info(f"Job {job_id}: Recovery: previews already complete on disk, nothing to enqueue")
        return True, "Recovery: preview manifests already present"
    except Exception as exc:
        log.error(f"Job {job_id}: Preview recovery failed: {exc}", exc_info=True)
        return False, f"Recovery failed: {exc}"


def _recover_postprocess_state(job, db: Session, error_reason: str) -> Tuple[bool, Optional[str]]:
    """Recover from state transition failure by resetting post-process state."""
    job_id = str(job.id)
    
    try:
        # Use devmode to allow backward state transition. #365 step 5
        # dropped the post_state column; reset only the underlying
        # job_status. start_transfer below re-marks transfer_phase
        # ="preparing" via StageState.postprocess_started.
        apply_job_state_devmode(
            db,
            job,
            updates={
                "job_status": "pending",
                "error_reason": f"Recovery: {error_reason}",
            },
            reason="failure recovery: reset post-process state",
        )
        
        # Re-enqueue via the unified start_transfer worker (#365).
        task_result = start_transfer.delay(job_id)
        try:
            StageState.postprocess_started(db, job, reason="failure recovery: reset state and re-enqueued start_transfer")
        except Exception as state_exc:
            log.warning("Job %s: postprocess_started after state recovery enqueue failed: %s", job_id, state_exc)
        log.info(f"Job {job_id}: Recovery: Reset state and re-enqueued start_transfer (task_id={task_result.id if task_result else 'unknown'})")

        return True, f"Recovery: Reset state and re-enqueued (task_id={task_result.id if task_result else 'unknown'})"
    except Exception as exc:
        log.error(f"Job {job_id}: State recovery failed: {exc}", exc_info=True)
        return False, f"Recovery failed: {exc}"


def clear_recovery_attempts(job_id: str) -> None:
    """Clear recovery attempt counter for a job (called on successful completion)."""
    _recovery_attempts.pop(job_id, None)


def get_recovery_attempts(job_id: str) -> int:
    """Get the number of recovery attempts for a job."""
    return _recovery_attempts.get(job_id, 0)





