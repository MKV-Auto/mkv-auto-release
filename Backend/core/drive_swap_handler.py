"""Orchestration glue between :mod:`drive_identity`, :mod:`drive_swap_detector`,
and the rip-job persistence layer.

Called from the backend's udev event handlers (drive_eject, rescan_drives).
Maintains a module-global cache of the last-seen identity_map and, on each
udev event, recomputes the fresh map, compares, and fails any active rip
job whose ``Job.drive_by_id_serial`` no longer matches the physical drive
currently at the same mount_point.

The cache is intentionally in-memory only — backend restarts clear it and
the first udev event after restart is treated as a fresh observation
(no swaps reported). This is the safer failure mode: a false negative
(missed swap right after restart) is recoverable; a false positive
(failing a healthy rip) is not.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from core.drive_identity import DriveIdentity, build_identity_map
from core.drive_swap_detector import DriveSwap, detect_drive_swaps

logger = logging.getLogger(__name__)


_cache_lock = threading.RLock()
_last_identity_map: dict[str, DriveIdentity] = {}


def check_and_handle_swaps(db: Any) -> list[DriveSwap]:
    """Rebuild the identity_map, detect swaps against the cached prior map,
    fail any active rip jobs for swapped-away drives, then commit the new
    map as the cache.

    Returns the list of detected swaps for logging / event-stream emission.
    """

    fresh_map = build_identity_map()

    with _cache_lock:
        previous = dict(_last_identity_map)
        _last_identity_map.clear()
        _last_identity_map.update(fresh_map)

    swaps = detect_drive_swaps(previous, fresh_map)
    for swap in swaps:
        logger.warning(
            "drive_swap detected at %s: %s -> %s; failing any active rip jobs "
            "for the previous serial",
            swap.mount_point, swap.previous_serial, swap.current_serial,
        )
        _emit_swap_notification(swap)
        _fail_jobs_for_swapped_serial(db, swap)
    return swaps


def _emit_swap_notification(swap: DriveSwap) -> None:
    """Surface the swap through the unified notification system so both
    the WebUI toast and Discord (if configured) carry the same alert.

    Failures are swallowed and logged — notifications are observability, not
    a hard requirement for the swap-handling correctness path."""

    try:
        from core.notifications import emit_notification_sync

        emit_notification_sync(
            message=(
                f"Drive at {swap.mount_point} swapped to a different physical "
                "drive at the OS level; any in-flight rip on the old drive has "
                "been failed to prevent corrupted output."
            ),
            kind="error",
            level="action_required",
            title="Drive identity changed mid-session",
            id_key=f"drive_swap:{swap.mount_point}:{swap.current_serial}",
        )
    except Exception as exc:
        logger.warning("drive_swap notification dispatch failed: %s", exc)


def reset_cache_for_tests() -> None:
    """Clear the module-global identity cache. Intended for use only from
    test setup/teardown so each test starts from a known state."""

    with _cache_lock:
        _last_identity_map.clear()


def _fail_jobs_for_swapped_serial(db: Any, swap: DriveSwap) -> None:
    """Fail any active rip jobs whose ``drive_by_id_serial`` matches the
    previous serial. Defensive: any individual job-update exception is
    logged but does not block subsequent updates."""

    try:
        from api import models as db_models
    except Exception as imp_exc:  # pragma: no cover
        logger.error("drive_swap_handler cannot import models: %s", imp_exc)
        return

    try:
        affected = (
            db.query(db_models.Job)
            .filter(
                db_models.Job.drive_by_id_serial == swap.previous_serial,
                db_models.Job.rip_state.in_(("pending", "running")),
                db_models.Job.dismissed.is_(False),
            )
            .all()
        )
    except Exception as query_exc:
        logger.error(
            "drive_swap_handler failed to query affected jobs for serial %s: %s",
            swap.previous_serial, query_exc,
        )
        return

    if not affected:
        return

    logger.warning(
        "drive_swap at %s: failing %d active rip job(s) for serial %s",
        swap.mount_point, len(affected), swap.previous_serial,
    )
    for job in affected:
        try:
            job.rip_state = "failed"
            job.job_status = "failed"
            job.error_reason = (
                f"Drive at {swap.mount_point} swapped to a different physical "
                f"drive mid-rip (previous serial {swap.previous_serial!r}, "
                f"now {swap.current_serial!r}); failing this job to prevent "
                "corrupted output."
            )
        except Exception as upd_exc:
            logger.error(
                "drive_swap_handler failed to mark job %s as failed: %s",
                getattr(job, "id", "?"), upd_exc,
            )

    try:
        db.commit()
    except Exception as commit_exc:
        logger.error(
            "drive_swap_handler commit failed after %d job updates: %s",
            len(affected), commit_exc,
        )
        try:
            db.rollback()
        except Exception:
            pass
