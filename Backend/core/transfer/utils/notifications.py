"""
Transfer notification system for alerting users of transfer events.
Calls core.notifications.emit_notification_sync so toasts and Discord receive transfer events.
"""
from typing import Optional
import logging

log = logging.getLogger(__name__)


def should_notify(config, event_type: str) -> bool:
    """
    Check if a transfer notification should be emitted.

    Historically gated on the per-config ``enable_notifications`` toggle.
    That column was dropped as redundant with the global Settings ->
    Notifications preferences (which control per-category x per-channel
    delivery). This helper now simply confirms we have a config to report
    against; downstream filtering is Settings-controlled.

    Args:
        config: TransferConfig instance
        event_type: Event type (started, completed, failed, etc.)

    Returns:
        True if a notification should be attempted for this event.
    """
    return bool(config)


def notify_transfer_started(job_id: str, config, file_name: Optional[str] = None) -> None:
    """
    Send notification when transfer starts.

    Args:
        job_id: Job ID
        config: TransferConfig instance
        file_name: Name of file being transferred (optional)
    """
    if not should_notify(config, "started"):
        return

    message = f"Transfer started for job {job_id}"
    if file_name:
        message += f": {file_name}"

    _send_notification(message, "info", "transfer_started", job_id)


def notify_transfer_completed(
    job_id: str,
    config,
    duration: float,
    speed: float,
    file_name: Optional[str] = None
) -> None:
    """
    Send notification when transfer completes successfully.
    
    Args:
        job_id: Job ID
        config: TransferConfig instance
        duration: Transfer duration in seconds
        speed: Average speed in MB/s
        file_name: Name of file transferred (optional)
    """
    if not should_notify(config, "completed"):
        return
    
    duration_str = f"{duration:.1f}s" if duration < 60 else f"{duration/60:.1f}m"
    message = f"Transfer completed for job {job_id}"
    if file_name:
        message += f": {file_name}"
    message += f" ({duration_str}, {speed:.2f} MB/s)"

    _send_notification(message, "success", "transfer_completed", job_id)


def notify_transfer_failed(
    job_id: str,
    config,
    error: str,
    file_name: Optional[str] = None
) -> None:
    """
    Send notification when transfer fails.
    
    Args:
        job_id: Job ID
        config: TransferConfig instance
        error: Error message
        file_name: Name of file that failed (optional)
    """
    if not should_notify(config, "failed"):
        return
    
    message = f"Transfer failed for job {job_id}"
    if file_name:
        message += f": {file_name}"
    message += f" - {error}"

    _send_notification(message, "error", "transfer_failed", job_id)


def notify_verification_failed(
    job_id: str,
    config,
    expected_hash: str,
    actual_hash: str,
    file_name: Optional[str] = None
) -> None:
    """
    Send notification when hash verification fails.
    
    Args:
        job_id: Job ID
        config: TransferConfig instance
        expected_hash: Expected hash
        actual_hash: Actual hash
        file_name: Name of file (optional)
    """
    if not should_notify(config, "verification_failed"):
        return
    
    message = f"Hash verification failed for job {job_id}"
    if file_name:
        message += f": {file_name}"
    message += f" (expected {expected_hash[:8]}..., got {actual_hash[:8]}...)"

    _send_notification(message, "error", "transfer_failed", job_id)


def notify_health_check_failed(
    config_id: str,
    check_type: str,
    error: str
) -> None:
    """
    Send notification when health check fails.
    
    Args:
        config_id: Transfer config ID
        check_type: Type of check that failed
        error: Error message
    """
    message = f"Health check failed for transfer config {config_id}: {check_type} - {error}"
    _send_notification(message, "warning", "error_generic", job_id=None)


def _send_notification(
    message: str,
    kind: str,
    level: str,
    job_id: Optional[str] = None,
) -> None:
    """
    Log and send notification via core.notifications (WebSocket + Discord).

    Args:
        message: Notification message
        kind: Toast kind (info, success, error, warning)
        level: Notification level for filtering (e.g. transfer_started, transfer_completed)
        job_id: Optional job id for envelope and dedupe
    """
    if kind == "error":
        log.error("[NOTIFICATION] %s", message)
    elif kind == "warning":
        log.warning("[NOTIFICATION] %s", message)
    else:
        log.info("[NOTIFICATION] %s", message)
    try:
        from core.notifications import emit_notification_sync
        emit_notification_sync(message, kind, level, job_id=job_id)
    except Exception as e:
        log.warning("Failed to emit transfer notification: %s", e)











