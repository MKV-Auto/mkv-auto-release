"""
API process startup: drive enumeration plus per-loaded-disc insert scans.

Must run only after disc_manager backend callbacks are registered so SSE /
coordinator state stay in sync with disc_cache.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import HTTPException

from core.logging_utils import get_logger
from core.utils import (
    is_registration_related_makemkv_failure,
    startup_warm_drive_cache,
)

logger = get_logger("core.startup_discs")

_warmup_state_lock = threading.RLock()
# After post-install warmup fails with registration-related MakeMKV error, retry once key is saved.
_drive_warmup_pending_after_key = False
# Last non-success classification: None | "registration_required" | "makemkv_error"
_last_warmup_error_kind: str | None = None


def drive_warmup_pending_after_key() -> bool:
    with _warmup_state_lock:
        return _drive_warmup_pending_after_key


def get_warmup_state() -> tuple[bool, str | None]:
    """(pending_retry_after_key, last_error_kind)."""
    with _warmup_state_lock:
        return _drive_warmup_pending_after_key, _last_warmup_error_kind


def record_drive_warmup_result(exc: BaseException | None) -> None:
    """
    Update pending-key flag and last error kind after a drive warmup attempt.
    """
    global _drive_warmup_pending_after_key, _last_warmup_error_kind
    with _warmup_state_lock:
        if exc is None:
            _drive_warmup_pending_after_key = False
            _last_warmup_error_kind = None
            return
        if is_registration_related_makemkv_failure(exc):
            _drive_warmup_pending_after_key = True
            _last_warmup_error_kind = "registration_required"
        else:
            _drive_warmup_pending_after_key = False
            _last_warmup_error_kind = "makemkv_error"


def disc_workflow_block_fields(
    validation: dict,
    registration_expired: bool,
) -> dict[str, str | bool]:
    """
    Derive disc scan/rip gating for API health (non-disc UI may stay usable).
    """
    pending, err_kind = get_warmup_state()

    if not validation.get("is_valid"):
        return {"disc_workflow_blocked": True, "disc_workflow_block_reason": "makemkv_not_installed"}
    if registration_expired:
        return {"disc_workflow_blocked": True, "disc_workflow_block_reason": "registration_required"}
    if pending:
        return {"disc_workflow_blocked": True, "disc_workflow_block_reason": "registration_required"}
    if err_kind == "makemkv_error":
        return {"disc_workflow_blocked": True, "disc_workflow_block_reason": "makemkv_error"}
    return {"disc_workflow_blocked": False, "disc_workflow_block_reason": "none"}


def _rip_active_at_mount(mount_point: str) -> bool:
    """Return True if ``_recover_inflight_jobs`` has already restarted a rip
    on ``mount_point``. Imported lazily so this module stays cheap to import."""
    try:
        from core.disc_locks import OPERATION_RIP, is_operation_active

        return is_operation_active(mount_point, OPERATION_RIP)
    except Exception:
        # If the lock layer is unavailable for any reason, fall through to
        # the scan — we'd rather risk a single MSG:5010 than silently skip
        # disc-insert handling for every drive.
        return False


def _run_insert_scan(disc_idx: str, mount_point: str) -> None:
    if _rip_active_at_mount(mount_point):
        # #562 PR 3: ``_recover_inflight_jobs`` already restarted a rip on
        # this drive — running ``info dev:`` against it now would race with
        # the in-flight ``mkv dev:`` and emit MSG:5010. Defer to whichever
        # post-rip path runs next (rip-complete callback or the next udev
        # change event).
        logger.info(
            "Startup insert-scan deferred for mount_point=%s disc_idx=%s: rip in progress",
            mount_point,
            disc_idx,
        )
        return

    from core._drive_operations import handle_disc_insert

    try:
        handle_disc_insert(str(disc_idx), mount_point)
    except HTTPException as exc:
        logger.warning(
            "Startup insert-scan HTTP error for mount_point=%s disc_idx=%s: %s",
            mount_point,
            disc_idx,
            exc.detail,
        )
    except Exception as exc:
        logger.warning(
            "Startup insert-scan failed for mount_point=%s disc_idx=%s: %s",
            mount_point,
            disc_idx,
            exc,
            exc_info=True,
        )


def startup_enumerate_and_rescan_loaded_discs(
    *,
    reraise_if_registration_required: bool = False,
) -> list:
    """
    Run startup_warm_drive_cache once, then handle_disc_insert for each
    (makemkv_index, mount_point) so disc_cache and initial-state match trays
    that already hold media at boot.

    Set MKVAUTO_SKIP_STARTUP_DISC_RESCAN=1 to enumerate only (no hash/info per disc).
    """
    drives = startup_warm_drive_cache(
        reraise_if_registration_required=reraise_if_registration_required
    )
    if os.getenv("MKVAUTO_SKIP_STARTUP_DISC_RESCAN", "").strip().lower() in (
        "1",
        "true",
        "yes",
    ):
        logger.info(
            "MKVAUTO_SKIP_STARTUP_DISC_RESCAN set; skipping per-disc insert scans (%s drives)",
            len(drives),
        )
        return drives

    serial = os.getenv("MKVAUTO_STARTUP_DISC_RESCAN_SERIAL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if serial or len(drives) <= 1:
        for disc_idx, mount_point in drives:
            _run_insert_scan(str(disc_idx), mount_point)
        return drives

    max_workers = max(1, len(drives))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_run_insert_scan, str(disc_idx), mount_point)
            for disc_idx, mount_point in drives
        ]
        for fut in as_completed(futures):
            fut.result()

    return drives


def run_startup_drive_warmup_if_makemkv_ready() -> list:
    """
    Run full drive enumeration + optional per-disc insert scans only when makemkvcon
    is present and executable. Records success/failure for post-key retry and health.
    """
    from core.makemkv_updater import validate_makemkv_installation

    v = validate_makemkv_installation()
    if not v.get("is_valid"):
        logger.info(
            "Skipping startup drive warmup: MakeMKV not ready (%s)",
            v.get("error_message") or "unknown",
        )
        return []
    try:
        drives = startup_enumerate_and_rescan_loaded_discs(
            reraise_if_registration_required=True,
        )
        record_drive_warmup_result(None)
        # #310: Warn if no optical drives were detected at startup
        if not drives:
            logger.warning("No optical drives detected at startup — check Docker device mapping (--device /dev/srN)")
            try:
                from core.notifications import emit_notification_sync
                emit_notification_sync(
                    "No optical drives detected. Check that drives are passed to the container (--device /dev/srN).",
                    "warning",
                    "error_generic",
                    title="No drives found",
                )
            except Exception:
                pass
        else:
            # #578: USB bandwidth contention check. Multiple optical drives on
            # a single sub-SuperSpeed bus saturate the controller during
            # concurrent rips and cascade into USB resets. Notify the user
            # once at startup so they can re-cable before kicking off a
            # multi-rip workflow.
            try:
                from core.notifications import emit_notification_sync
                from core.usb_topology import detect_contention_warnings, detect_optical_drives

                warnings = detect_contention_warnings(detect_optical_drives())
                for w in warnings:
                    logger.warning(
                        "USB bandwidth contention detected: bus=%s speed=%sMbps drives=%s",
                        w.bus, w.speed_mbps, w.drives,
                    )
                    emit_notification_sync(
                        w.message,
                        "warning",
                        "action_required",
                        title="USB bandwidth contention",
                        id_key=f"usb_bus_contention:{w.bus}",
                    )
            except Exception as exc:
                logger.debug("USB topology check failed (non-fatal): %s", exc)
        return drives
    except Exception as exc:
        record_drive_warmup_result(exc)
        logger.warning("Startup drive warmup failed: %s", exc, exc_info=True)
        return []
