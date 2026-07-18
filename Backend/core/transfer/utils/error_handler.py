"""
Transfer error handling and retry logic.
"""
from typing import Dict, Any, Optional
from sqlalchemy.orm import Session
from api import models
import logging

log = logging.getLogger(__name__)


def handle_transfer_error(
    db: Session,
    job_id: str,
    error: Exception,
    config: models.TransferConfig
) -> None:
    """
    Handle transfer errors and update job status.
    
    Args:
        db: Database session
        job_id: Job ID
        error: Exception that occurred
        config: TransferConfig instance
    """
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        return
    
    error_category = categorize_error(error)
    error_message = str(error)
    
    # Update transfer state only — do NOT fail the overall job so the user
    # can fix the destination and retry without re-ripping.
    job.transfer_state = "failed"
    job.transfer_error = error_message
    
    # Increment retry count if applicable
    if can_retry_automatically(error_category):
        job.transfer_retry_count += 1
    
    db.commit()
    
    log.error(f"[{job_id}] Transfer error ({error_category}): {error_message}")


def categorize_error(error: Exception) -> str:
    """
    Categorize error type.
    
    Args:
        error: Exception instance
        
    Returns:
        Error category string
    """
    error_str = str(error).lower()
    error_type = type(error).__name__
    
    # Connection errors
    if any(keyword in error_str for keyword in ["connection", "network", "timeout", "unreachable", "refused"]):
        return "connection"
    
    # Authentication errors
    if any(keyword in error_str for keyword in ["authentication", "auth", "permission denied", "access denied", "unauthorized"]):
        return "authentication"
    
    # Permission errors
    if any(keyword in error_str for keyword in ["permission", "access denied", "read-only", "write"]):
        return "permission"
    
    # Space errors
    if any(keyword in error_str for keyword in ["space", "disk full", "no space", "quota"]):
        return "space"
    
    # Hash verification errors
    if any(keyword in error_str for keyword in ["hash", "verification", "checksum", "corrupt"]):
        return "verification"
    
    # Conflict errors
    if any(keyword in error_str for keyword in ["exists", "conflict", "file exists"]):
        return "conflict"
    
    # Validation errors
    if any(keyword in error_str for keyword in ["validation", "invalid", "missing"]):
        return "validation"
    
    return "unknown"


def can_retry(job_id: str, db: Session) -> bool:
    """
    Check if retry is allowed for a job.
    
    Args:
        job_id: Job ID
        db: Database session
        
    Returns:
        True if retry is allowed
    """
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        return False
    
    if job.transfer_retry_count >= job.transfer_max_retries:
        return False
    
    return True


def can_retry_automatically(error_category: str) -> bool:
    """
    Check if error can be retried automatically.
    
    Args:
        error_category: Error category
        
    Returns:
        True if automatic retry is appropriate
    """
    # Transient errors that can be retried automatically
    automatic_retry_categories = ["connection", "timeout"]
    return error_category in automatic_retry_categories


def retry_transfer(db: Session, job_id: str) -> None:
    """
    Retry a failed transfer (user-initiated).

    Resets transfer state and retry counter so the user gets fresh auto-retry
    attempts after fixing the underlying issue (e.g. NAS back online).
    """
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        raise ValueError(f"Job not found: {job_id}")
    
    if job.transfer_state not in ("failed", "pending"):
        raise ValueError(f"Transfer is not in a retryable state (current: {job.transfer_state})")
    
    # Reset transfer state and retry counter (user-initiated gets fresh attempts)
    job.transfer_state = "pending"
    job.transfer_error = None
    job.transfer_progress = 0
    job.transfer_retry_count = 0
    
    db.commit()
    
    log.info(f"[{job_id}] User-initiated transfer retry (counter reset to 0)")


def get_transfer_error_details(job_id: str, db: Session) -> Dict[str, Any]:
    """
    Get detailed error information for a failed transfer.
    
    Args:
        job_id: Job ID
        db: Database session
        
    Returns:
        Dictionary with error details
    """
    job = db.query(models.Job).filter(models.Job.id == job_id).first()
    if not job:
        return {"error": "Job not found"}
    
    error_category = "unknown"
    if job.transfer_error:
        # Categorize the error
        error_category = categorize_error(Exception(job.transfer_error))
    
    return {
        "job_id": job_id,
        "error_message": job.transfer_error,
        "error_category": error_category,
        "retry_count": job.transfer_retry_count,
        "max_retries": job.transfer_max_retries,
        "can_retry": can_retry(job_id, db),
        "can_retry_automatically": can_retry_automatically(error_category),
    }











