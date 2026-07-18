"""
Transfer history and audit logging system.
"""
from typing import List, Dict, Any, Optional
from sqlalchemy.orm import Session, joinedload
from api import models
import logging

log = logging.getLogger(__name__)


def log_transfer_start(
    db: Session,
    job_id: Optional[str],
    config_id: Optional[str],
    mode: str,
    source_path: str,
    dest_path: str
) -> str:
    """
    Create a transfer history entry when transfer starts.
    
    Args:
        db: Database session
        job_id: Job ID (optional)
        config_id: Transfer config ID (optional)
        mode: Transfer mode (local, rsync, smb, nfs)
        source_path: Source path
        dest_path: Destination path
        
    Returns:
        History entry ID
    """
    history = models.TransferHistory(
        job_id=job_id,
        transfer_config_id=config_id,
        mode=mode,
        source_path=source_path,
        destination_path=dest_path,
        status="running",
    )
    db.add(history)
    db.commit()
    db.refresh(history)
    return history.id


def log_transfer_progress(
    db: Session,
    history_id: str,
    bytes_transferred: int,
    speed_mbps: float
) -> None:
    """
    Update transfer progress in history entry.
    
    Args:
        db: Database session
        history_id: History entry ID
        bytes_transferred: Bytes transferred so far
        speed_mbps: Current speed in MB/s
    """
    history = db.query(models.TransferHistory).filter(models.TransferHistory.id == history_id).first()
    if history:
        history.bytes_transferred = bytes_transferred
        history.average_speed_mbps = speed_mbps
        db.commit()


def log_transfer_complete(
    db: Session,
    history_id: str,
    bytes_transferred: int,
    duration: float,
    verified: bool,
    hash_value: Optional[str] = None
) -> None:
    """
    Mark transfer as completed in history.
    
    Args:
        db: Database session
        history_id: History entry ID
        bytes_transferred: Total bytes transferred
        duration: Transfer duration in seconds
        verified: Whether hash verification passed
        hash_value: Hash value used for verification
    """
    history = db.query(models.TransferHistory).filter(models.TransferHistory.id == history_id).first()
    if history:
        history.status = "completed"
        history.bytes_transferred = bytes_transferred
        history.transfer_duration_seconds = duration
        history.average_speed_mbps = bytes_transferred / duration / (1024 * 1024) if duration > 0 else 0
        history.verification_status = "verified" if verified else "skipped"
        history.verification_hash = hash_value
        db.commit()


def log_transfer_failed(
    db: Session,
    history_id: str,
    error: str
) -> None:
    """
    Mark transfer as failed in history.
    
    Args:
        db: Database session
        history_id: History entry ID
        error: Error message
    """
    history = db.query(models.TransferHistory).filter(models.TransferHistory.id == history_id).first()
    if history:
        history.status = "failed"
        history.error_message = error
        db.commit()


def log_transfer_skipped(
    db: Session,
    history_id: str,
    reason: str
) -> None:
    """
    Mark transfer as skipped in history.
    
    Args:
        db: Database session
        history_id: History entry ID
        reason: Reason for skipping
    """
    history = db.query(models.TransferHistory).filter(models.TransferHistory.id == history_id).first()
    if history:
        history.status = "skipped"
        history.error_message = reason
        db.commit()


def log_transfer_deduplicated(
    db: Session,
    history_id: str,
    existing_hash: str
) -> None:
    """
    Mark transfer as deduplicated in history.
    
    Args:
        db: Database session
        history_id: History entry ID
        existing_hash: Hash of existing file
    """
    history = db.query(models.TransferHistory).filter(models.TransferHistory.id == history_id).first()
    if history:
        history.status = "deduplicated"
        history.was_deduplicated = True
        history.verification_hash = existing_hash
        db.commit()


def get_transfer_history(
    db: Session,
    job_id: Optional[str] = None,
    config_id: Optional[str] = None,
    limit: int = 100
) -> List[models.TransferHistory]:
    """
    Query transfer history.

    Eager-loads the Job → Disc → Release → Movie chain so the API response
    can surface human-readable identity (movie_name, release_name,
    release_year, disc_name) for each row without an N+1 follow-up per row
    (#593). Pattern mirrors `unfinished_jobs.query_unfinished_jobs`.

    Orphaned rows (job_id IS NULL after job deletion's SET NULL) keep their
    identity fields as None — the router resolves to "(orphaned)" downstream.

    Args:
        db: Database session
        job_id: Filter by job ID (optional)
        config_id: Filter by config ID (optional)
        limit: Maximum number of results

    Returns:
        List of transfer history entries with eager-loaded Job/Disc/Release/Movie
    """
    query = (
        db.query(models.TransferHistory)
        .options(
            joinedload(models.TransferHistory.job)
            .joinedload(models.Job.disc)
            .joinedload(models.Disc.release)
            .joinedload(models.Release.movie)
        )
    )

    if job_id:
        query = query.filter(models.TransferHistory.job_id == job_id)
    if config_id:
        query = query.filter(models.TransferHistory.transfer_config_id == config_id)

    return query.order_by(models.TransferHistory.created_at.desc()).limit(limit).all()


def get_transfer_statistics(
    db: Session,
    config_id: Optional[str] = None,
    days: int = 30
) -> Dict[str, Any]:
    """
    Get transfer statistics.
    
    Args:
        db: Database session
        config_id: Filter by config ID (optional)
        days: Number of days to look back
        
    Returns:
        Dictionary with statistics
    """
    from datetime import datetime, timedelta
    from sqlalchemy import func
    
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    query = db.query(models.TransferHistory).filter(
        models.TransferHistory.created_at >= cutoff_date
    )
    
    if config_id:
        query = query.filter(models.TransferHistory.transfer_config_id == config_id)
    
    total = query.count()
    completed = query.filter(models.TransferHistory.status == "completed").count()
    failed = query.filter(models.TransferHistory.status == "failed").count()
    deduplicated = query.filter(models.TransferHistory.was_deduplicated == True).count()
    
    # Calculate average speed
    avg_speed_result = query.filter(
        models.TransferHistory.status == "completed",
        models.TransferHistory.average_speed_mbps.isnot(None)
    ).with_entities(func.avg(models.TransferHistory.average_speed_mbps)).scalar()
    
    avg_speed = float(avg_speed_result) if avg_speed_result else 0.0
    
    # Calculate total bytes transferred
    total_bytes_result = query.filter(
        models.TransferHistory.status == "completed",
        models.TransferHistory.bytes_transferred.isnot(None)
    ).with_entities(func.sum(models.TransferHistory.bytes_transferred)).scalar()
    
    total_bytes = int(total_bytes_result) if total_bytes_result else 0
    
    success_rate = (completed / total * 100) if total > 0 else 0.0
    
    return {
        "total_transfers": total,
        "completed": completed,
        "failed": failed,
        "deduplicated": deduplicated,
        "success_rate": round(success_rate, 2),
        "average_speed_mbps": round(avg_speed, 2),
        "total_bytes_transferred": total_bytes,
        "period_days": days,
    }











