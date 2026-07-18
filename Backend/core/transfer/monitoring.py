"""
Transfer monitoring system combining health checks, speed tracking, and cleanup.
Combines transfer_health.py, transfer_speed.py, and transfer_cleanup.py.
"""
import time
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.orm import Session
from api import models
from datetime import datetime, timedelta
import logging

log = logging.getLogger(__name__)


# ===== Health Monitoring Functions (from transfer_health.py) =====

def check_destination_health(db: Session, config) -> Dict[str, Any]:
    """
    Run all health checks for a transfer destination.
    
    Args:
        db: Database session
        config: TransferConfig instance
        
    Returns:
        Dictionary with health check results
    """
    results = {
        "overall": {"status": "unknown", "message": "", "response_time_ms": None},
        "connectivity": {"status": "unknown", "message": "", "response_time_ms": None},
        "authentication": {"status": "unknown", "message": "", "response_time_ms": None},
        "permissions": {"status": "unknown", "message": "", "response_time_ms": None},
        "space": {"status": "unknown", "message": "", "response_time_ms": None},
    }
    
    # Check connectivity
    start_time = time.time()
    try:
        from core.transfer.validation import validate_connectivity
        passed, error = validate_connectivity(db, config)
        elapsed_ms = int((time.time() - start_time) * 1000)
        results["connectivity"] = {
            "status": "healthy" if passed else "unhealthy",
            "message": error if not passed else "Connection successful",
            "response_time_ms": elapsed_ms,
        }
    except Exception as e:
        elapsed_ms = int((time.time() - start_time) * 1000)
        results["connectivity"] = {
            "status": "unhealthy",
            "message": str(e),
            "response_time_ms": elapsed_ms,
        }
    
    # Check authentication (for remote transfers)
    if config.mode != "local":
        start_time = time.time()
        try:
            from core.transfer.validation import validate_authentication
            passed, error = validate_authentication(db, config)
            elapsed_ms = int((time.time() - start_time) * 1000)
            results["authentication"] = {
                "status": "healthy" if passed else "unhealthy",
                "message": error if not passed else "Authentication successful",
                "response_time_ms": elapsed_ms,
            }
        except Exception as e:
            elapsed_ms = int((time.time() - start_time) * 1000)
            results["authentication"] = {
                "status": "unhealthy",
                "message": str(e),
                "response_time_ms": elapsed_ms,
            }
    else:
        results["authentication"] = {
            "status": "healthy",
            "message": "N/A for local transfers",
            "response_time_ms": 0,
        }
    
    # Calculate overall health
    all_checks = [r["status"] for r in results.values() if r["status"] != "unknown"]
    if not all_checks:
        results["overall"]["status"] = "unknown"
    elif all(s == "healthy" for s in all_checks):
        results["overall"]["status"] = "healthy"
    elif any(s == "unhealthy" for s in all_checks):
        results["overall"]["status"] = "unhealthy"
    else:
        results["overall"]["status"] = "degraded"
    
    return results


def record_health_check(db: Session, config_id: str, results: Dict[str, Any]) -> None:
    """
    Store health check results in database.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        results: Health check results dictionary
    """
    for check_type, result in results.items():
        health_check = models.TransferHealthCheck(
            transfer_config_id=config_id,
            check_type=check_type,
            status=result["status"],
            message=result["message"],
            response_time_ms=result.get("response_time_ms"),
        )
        db.add(health_check)
    
    db.commit()


def get_health_status(db: Session, config_id: str) -> Dict[str, Any]:
    """
    Get latest health status for a transfer config.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        
    Returns:
        Dictionary with latest health status
    """
    # Get most recent health check for each check type
    from sqlalchemy import func
    
    # Use a subquery to find the max checked_at per check_type, then join back
    subq = db.query(
        models.TransferHealthCheck.check_type,
        func.max(models.TransferHealthCheck.checked_at).label('max_checked_at')
    ).filter(
        models.TransferHealthCheck.transfer_config_id == config_id
    ).group_by(
        models.TransferHealthCheck.check_type
    ).subquery()
    
    # Join with the main table to get the full records
    latest_checks = db.query(models.TransferHealthCheck).join(
        subq,
        (models.TransferHealthCheck.check_type == subq.c.check_type) &
        (models.TransferHealthCheck.checked_at == subq.c.max_checked_at)
    ).filter(
        models.TransferHealthCheck.transfer_config_id == config_id
    ).all()
    
    status = {}
    for latest in latest_checks:
        status[latest.check_type] = {
            "status": latest.status,
            "message": latest.message,
            "response_time_ms": latest.response_time_ms,
            "checked_at": latest.checked_at.isoformat() if latest.checked_at else None,
        }
    
    return status


def get_health_history(db: Session, config_id: str, days: int = 7) -> List[Dict[str, Any]]:
    """
    Get health check history.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        days: Number of days to look back
        
    Returns:
        List of health check records
    """
    cutoff_date = datetime.utcnow() - timedelta(days=days)
    
    checks = db.query(models.TransferHealthCheck).filter(
        models.TransferHealthCheck.transfer_config_id == config_id,
        models.TransferHealthCheck.checked_at >= cutoff_date
    ).order_by(
        models.TransferHealthCheck.checked_at.desc()
    ).all()
    
    return [
        {
            "id": check.id,
            "check_type": check.check_type,
            "status": check.status,
            "message": check.message,
            "response_time_ms": check.response_time_ms,
            "checked_at": check.checked_at.isoformat() if check.checked_at else None,
        }
        for check in checks
    ]


def should_alert_on_health(db: Session, config_id: str) -> bool:
    """
    Determine if health issues warrant an alert.
    
    Args:
        db: Database session
        config_id: Transfer config ID
        
    Returns:
        True if alert should be sent
    """
    status = get_health_status(db, config_id)
    
    # Alert if overall status is unhealthy
    overall = status.get("overall", {})
    if overall.get("status") == "unhealthy":
        return True
    
    # Alert if connectivity is unhealthy
    connectivity = status.get("connectivity", {})
    if connectivity.get("status") == "unhealthy":
        return True
    
    return False


# ===== Speed Tracking Functions (from transfer_speed.py) =====

def calculate_speed(bytes_transferred: int, elapsed_time: float) -> float:
    """
    Calculate transfer speed in MB/s.
    
    Args:
        bytes_transferred: Number of bytes transferred
        elapsed_time: Elapsed time in seconds
        
    Returns:
        Speed in MB/s (megabytes per second)
    """
    if elapsed_time <= 0:
        return 0.0
    
    bytes_per_second = bytes_transferred / elapsed_time
    mb_per_second = bytes_per_second / (1024 * 1024)
    return round(mb_per_second, 2)


class SpeedTracker:
    """Track transfer speed over time."""
    
    def __init__(self):
        self.start_time: Optional[float] = None
        self.bytes_transferred: int = 0
        self.last_update_time: Optional[float] = None
        self.last_bytes: int = 0
    
    def start(self):
        """Start tracking."""
        self.start_time = time.time()
        self.last_update_time = self.start_time
        self.bytes_transferred = 0
        self.last_bytes = 0
    
    def update(self, bytes_transferred: int) -> float:
        """
        Update with current bytes transferred.
        
        Args:
            bytes_transferred: Current total bytes transferred
            
        Returns:
            Current speed in MB/s
        """
        self.bytes_transferred = bytes_transferred
        current_time = time.time()
        
        if self.last_update_time is None:
            self.last_update_time = current_time
            self.last_bytes = bytes_transferred
            return 0.0
        
        # Calculate speed based on recent progress
        elapsed = current_time - self.last_update_time
        bytes_delta = bytes_transferred - self.last_bytes
        
        self.last_update_time = current_time
        self.last_bytes = bytes_transferred
        
        return calculate_speed(bytes_delta, elapsed)
    
    def get_average_speed(self) -> float:
        """
        Get average speed since start.
        
        Returns:
            Average speed in MB/s
        """
        if self.start_time is None:
            return 0.0
        
        elapsed = time.time() - self.start_time
        return calculate_speed(self.bytes_transferred, elapsed)
    
    def get_elapsed_time(self) -> float:
        """Get elapsed time in seconds."""
        if self.start_time is None:
            return 0.0
        return time.time() - self.start_time


# ===== Cleanup Functions (from transfer_cleanup.py) =====

def should_cleanup(job_id: str, config) -> bool:
    """
    Return True — post-transfer source cleanup is now unconditional.

    The per-config ``cleanup_source`` toggle used to gate this, but the
    ``reconcile_job_mkv_cleanup`` periodic task in ``workers/tasks.py``
    already reaps completed/failed jobs unconditionally after 1 hour, so
    the toggle only controlled whether cleanup ran synchronously or up to
    an hour later. Aligning UI reality with runtime reality: always clean
    up synchronously after a successful, verified transfer.
    """
    return True


def cleanup_source_safe(job_id: str, config, source_paths: list[Path]) -> Tuple[bool, str]:
    """
    Safely clean up source files after successful transfer.
    
    Args:
        job_id: Job ID
        config: TransferConfig instance
        source_paths: List of source file paths to clean up
        
    Returns:
        Tuple of (success, error_message)
    """
    if not should_cleanup(job_id, config):
        return True, ""
    
    if not source_paths:
        return True, ""
    
    errors = []
    cleaned = []
    
    for source_path in source_paths:
        if not source_path.exists():
            log.warning(f"[{job_id}] Source file does not exist, skipping cleanup: {source_path}")
            continue
        
        try:
            if source_path.is_file():
                source_path.unlink()
                cleaned.append(str(source_path))
                log.info(f"[{job_id}] Cleaned up source file: {source_path}")
            elif source_path.is_dir():
                # For directories, remove recursively
                shutil.rmtree(source_path)
                cleaned.append(str(source_path))
                log.info(f"[{job_id}] Cleaned up source directory: {source_path}")
        except Exception as e:
            error_msg = f"Failed to cleanup {source_path}: {str(e)}"
            errors.append(error_msg)
            log.error(f"[{job_id}] {error_msg}")
    
    if errors:
        return False, "; ".join(errors)
    
    return True, f"Cleaned up {len(cleaned)} source file(s)"


def cleanup_source(job_id: str, config, source_paths: list[Path]) -> None:
    """
    Clean up source files (raises exception on error).
    
    Args:
        job_id: Job ID
        config: TransferConfig instance
        source_paths: List of source file paths to clean up
    """
    success, error = cleanup_source_safe(job_id, config, source_paths)
    if not success:
        raise RuntimeError(f"Cleanup failed: {error}")
