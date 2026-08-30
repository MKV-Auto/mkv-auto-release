# api/routers/events.py
import datetime
import json
import asyncio
import os
import logging
import re
import time
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
from filelock import FileLock, Timeout

from core.loop_local    import LoopLocalEvent, LoopLocalLock
from core.utils         import MakeMKVError, get_drive_scan_lock_path, build_drive_api_dict
from core import makemkv_state
from core.makemkv_update_jobs import get_job
from core.disc_cache    import (
    get as get_cached,
    set as set_cached,
    clear_key as clear_cached_key,
    get_by_mount_point as get_cached_by_mount_point,
    clear_keys_by_mount_point as clear_cached_by_mount_point,
)
from core.disc_manager import list_drives, get_disc_info, refresh_disc_info as refresh_disc_info_dm
from core.job_paths import JobPaths
from core.drive_manager_client import DriveManagerError
from api import crud, database
from pathlib import Path
from api import scan_guard
from api.schemas        import DriveInfo, DiscDetail
from api.routers.jobs   import _derive_pipeline
from api.routers.jobs   import preview_queue as preview_queue_snapshot
from api.routers.discs  import _infer_disc_format  # reuse format inference for enrichment
from parsing.disc_parser import hydrate_disc_payload
from core.utils import is_dev_mode
from core.logging_utils import get_logger
from typing import Optional, Callable, Union, Any

router = APIRouter(prefix="/events", tags=["events"])
logger = get_logger("api.routers.events")
DRIVE_SCAN_TIMEOUT = float(os.getenv("DRIVE_SCAN_TIMEOUT", "-1"))  # seconds; <=0 disables timeout
DISABLE_AUTOSCAN = os.getenv("MKVAUTO_DISABLE_AUTOSCAN", "").lower() in ("1", "true", "yes")
_last_drives: dict[str, Any] = {"ts": 0, "drives": []}
# Loop-local throughout this module: a bare asyncio primitive kept in module
# state sticks to the first event loop that contends on it. See core/loop_local.py.
_drive_scan_lock = LoopLocalLock()


def _coerce_drive_entry(entry: Union[dict, tuple, list]) -> dict[str, Any]:
    """Normalize stored drive entries to dicts (supports legacy tuple list)."""
    if isinstance(entry, dict):
        d = dict(entry)
        dn = str(d.get("disc_num") or "")
        mp = str(d.get("mount_point") or "")
        d["disc_num"] = dn
        d["mount_point"] = mp
        d.setdefault("makemkv_disc_index", str(d.get("makemkv_disc_index") or dn))
        d.setdefault("drive_hardware_name", d.get("drive_hardware_name") or "")
        d.setdefault("friendly_label", d.get("friendly_label") or "")
        d.setdefault("name", d.get("name") or "")
        return d
    if isinstance(entry, (tuple, list)) and len(entry) >= 2:
        return build_drive_api_dict(str(entry[0]), str(entry[1]))
    return {
        "disc_num": "",
        "mount_point": "",
        "makemkv_disc_index": "",
        "drive_hardware_name": "",
        "friendly_label": "",
        "name": "",
    }


def _normalize_drive_list(drives: list) -> list[dict[str, Any]]:
    return [_coerce_drive_entry(x) for x in (drives or [])]


def _drive_payload_for_api(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "disc_num": d.get("disc_num", ""),
        "mount_point": d.get("mount_point", ""),
        "makemkv_disc_index": d.get("makemkv_disc_index", d.get("disc_num", "")),
        "drive_hardware_name": d.get("drive_hardware_name") or "",
        "friendly_label": d.get("friendly_label") or "",
        "name": d.get("name") or "",
    }
DRIVE_SCAN_FILELOCK = str(get_drive_scan_lock_path())
_drive_scan_event = LoopLocalEvent()
# Store pending ejection events to broadcast to all connected SSE clients
_pending_ejections: list[dict] = []
_ejection_lock = LoopLocalLock()
# Store pending drive changes and disc info updates to broadcast to SSE clients
_pending_drive_changes: list[dict] = []
_pending_discinfo_updates: list[dict] = []
_broadcast_lock = LoopLocalLock()
# Track active disc info loading tasks to avoid duplicates
_active_disc_loads: dict[str, asyncio.Task] = {}
_disc_load_lock = LoopLocalLock()
# Callback registry for Disc Manager notifications
_disc_manager_callbacks: dict[str, Callable] = {}
# Reference to FastAPI app for event loop access
_app_ref: Optional[object] = None
logger = logging.getLogger("api.routers.events")


def register_disc_manager_callback(name: str, callback: Callable) -> None:
    """
    Register a callback function that Disc Manager can call to notify Backend API.
    
    Args:
        name: Callback name (e.g., "disc_inserted", "disc_scan_complete")
        callback: Callable function to invoke
    """
    _disc_manager_callbacks[name] = callback
    logger.info(f"Registered Disc Manager callback: {name}")


def set_app_reference(app: object) -> None:
    """
    Store reference to FastAPI app for event loop access.
    
    Args:
        app: FastAPI application instance
    """
    global _app_ref
    _app_ref = app
    logger.info("Stored FastAPI app reference for event loop access")


def _notify_disc_inserted(disc_num: str, mount_point: str) -> None:
    """
    Notify Backend API that a disc has been inserted (early notification).
    Emits disc_inserted to coordinator with scan_state: 'pending'.
    
    This function can be called from sync context (Drive Manager thread).
    Uses asyncio.run_coroutine_threadsafe to bridge to async context.
    
    Args:
        disc_num: Disc number
        mount_point: Mount point (e.g., "/dev/sr1")
    """
    # Schedule async broadcast task
    if _app_ref and hasattr(_app_ref, 'state') and hasattr(_app_ref.state, 'event_loop'):
        loop = _app_ref.state.event_loop
        if loop.is_running():
            async def _broadcast_inserted():
                try:
                    from api.routers.websockets import _emit_to_coordinator
                    # Emit disc_inserted with scan_state: 'pending'
                    # Use temporary disc_id until disc is scanned
                    await _emit_to_coordinator("disc_inserted", {
                        "disc_id": f"pending-{disc_num}",  # Temporary ID until disc is scanned
                        "disc_num": disc_num,
                        "mount_point": mount_point,
                        "disc_hash": None,
                        "disc_state": "in_drive",
                        "scan_state": "pending",  # Disc is pending scan
                        "scan_error": None,
                    })
                    logger.info(f"Emitted disc_inserted with scan_state=pending for {disc_num} at {mount_point}")
                except Exception as exc:
                    logger.warning(f"Failed to emit disc_inserted to coordinator: {exc}")
            
            asyncio.run_coroutine_threadsafe(_broadcast_inserted(), loop)
        else:
            # Event loop not running, try to schedule directly
            try:
                asyncio.create_task(_broadcast_inserted())
            except RuntimeError:
                logger.warning("Could not schedule disc inserted notification: no event loop")
    else:
        logger.warning("Could not schedule disc inserted notification: no app reference")


def _schedule_on_app_loop(coro, description: str) -> bool:
    """Run *coro* on the FastAPI event loop from a sync (worker-thread) caller.

    Returns True when the coroutine was scheduled. Mirrors the bridging that
    :func:`_notify_disc_inserted` does inline, but reusable and safe to call
    from the drive-manager thread.
    """
    loop = getattr(getattr(_app_ref, "state", None), "event_loop", None)
    if loop is not None and loop.is_running():
        asyncio.run_coroutine_threadsafe(coro, loop)
        return True
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        logger.warning("Could not schedule %s: no running event loop", description)
        return False
    asyncio.ensure_future(coro)
    return True


def _notify_drive_unresponsive(
    mount_point: str,
    disc_num: str | None,
    message: str,
    code: str = "drive_unresponsive",
    *,
    notify: bool = True,
) -> None:
    """Surface a drive-level fault to the UI and the error notification channel.

    Called from the fail-closed path in ``core._drive_operations`` when a drive
    stops answering (#723 / #724). Two things happen, both best-effort:

    1. ``disc_scan_failed`` on the coordinator socket so the drive card shows
       the error instead of "Drive 0" or the previous disc's title.
    2. An ``errors``-bucket notification (in-app bell + Discord) so the user is
       told at the moment of detection rather than discovering it hours later.
    """
    disc_num_str = str(disc_num) if disc_num is not None else None

    async def _broadcast_failed() -> None:
        try:
            from api.routers.websockets import _emit_to_coordinator

            await _emit_to_coordinator("disc_scan_failed", {
                "disc_id": f"drive-error-{disc_num_str or mount_point}",
                "disc_num": disc_num_str,
                "mount_point": mount_point,
                "disc_hash": None,
                "disc_state": "in_drive",
                "scan_state": "failed",
                "scan_error": message,
                "drive_error_code": code,
                # #723: the drive card may still be carrying the PREVIOUS
                # disc's identity (that is the whole bug — a wedged drive
                # showing "Thor" while a different disc sits in the tray).
                # A plain disc_scan_failed only patches scan_state/scan_error,
                # so tell the client to drop the metadata outright. Empty-scan
                # failures deliberately omit this flag: their volume-label
                # info_title *is* read from the current disc and must survive.
                "clear_identity": True,
            })
            logger.info(
                "Emitted disc_scan_failed for unresponsive drive mount_point=%s code=%s",
                mount_point, code,
            )
        except Exception as exc:
            logger.warning("Failed to emit disc_scan_failed for %s: %s", mount_point, exc)

    _schedule_on_app_loop(_broadcast_failed(), f"drive error broadcast for {mount_point}")

    if not notify:
        return
    try:
        from core.drive_health import FAULT_NOTIFICATION_LEVEL, fault_notification_id_key
        from core.notifications import emit_notification_sync

        emit_notification_sync(
            message=f"{mount_point}: {message}",
            kind="error",
            level=FAULT_NOTIFICATION_LEVEL,
            title="Drive is not responding",
            # Device-scoped so a wedged drive alerts once per fault, not once
            # per rescan attempt. clear_drive_health() drops this dedupe window
            # on recovery, so a drive the user fixes and re-breaks alerts again
            # rather than waiting out the TTL.
            id_key=fault_notification_id_key(code, mount_point),
        )
    except Exception as exc:
        logger.warning("Failed to emit drive-unresponsive notification for %s: %s", mount_point, exc)


async def _notify_disc_scan_complete_async(disc_info: dict) -> None:
    """
    Async helper to handle disc scan completion notification.

    Args:
        disc_info: Enriched disc information dict
    """
    # Get disc_num from disc_info itself (the MakeMKV disc number), not from parameter
    # The disc_info dict should have the correct disc_num from the scan
    actual_disc_num = str(disc_info.get("disc_num", ""))
    mount_point = disc_info.get("mount_point", "")
    
    # Persist to database using crud module
    disc_hash = disc_info.get("disc_hash")
    try:
        db = database.SessionLocal()
        try:
            if disc_hash:
                disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, disc_info)
                crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
                
                # Enrich disc_info with database data
                disc_info["disc_id"] = str(disc_record.id)
                disc_info["disc_number"] = disc_record.disc_number
                disc_info["discdb_disc_num"] = getattr(disc_record, "discdb_disc_num", None)
                disc_info["disc_slug"] = disc_record.disc_slug
                disc_info["disc_name"] = disc_record.disc_name
                if disc_record.release_id:
                    disc_info["release_id"] = str(disc_record.release_id)
                    if disc_record.release:
                        disc_info["disc_group"] = disc_record.release.slug
                        disc_info["release_name"] = disc_record.release.name
            else:
                # No hash available - try to find existing disc by mount_point or create temporary record
                # This ensures we have a disc_id for WebSocket messages even when hash fails
                logger.info(f"No disc_hash for disc {actual_disc_num} at {mount_point}, attempting to find/create disc record")
                # Try to find existing disc by matching mount_point or disc_num in recent scans
                # For now, we'll generate a temporary disc_id to ensure WebSocket works
                # The disc will be properly persisted when hash becomes available
                import uuid
                temp_disc_id = str(uuid.uuid4())
                disc_info["disc_id"] = temp_disc_id
                logger.info(f"Generated temporary disc_id {temp_disc_id} for disc {actual_disc_num}")
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"Failed to persist disc to DB: {exc}")
        # Even if DB fails, generate a temporary disc_id for WebSocket messaging
        if "disc_id" not in disc_info:
            import uuid
            temp_disc_id = str(uuid.uuid4())
            disc_info["disc_id"] = temp_disc_id
            logger.info(f"Generated fallback disc_id {temp_disc_id} after DB error")
    
    # If DiscDB created/updated Movie or Release entities, notify the frontend
    # so cached selectors refresh without a full page reload (#346).
    if disc_hash and disc_info.get("discdb_hit"):
        try:
            from api.routers.discs import _emit_options_changed
            asyncio.create_task(_emit_options_changed())
        except Exception as opts_exc:
            logger.debug(f"Failed to emit options_changed after DiscDB scan: {opts_exc}")

    # Enrich and cache
    # Use actual_disc_num (already extracted from disc_info above)
    enriched = _hydrate_and_enrich(
        disc_info,
        actual_disc_num,
        mount_point
    )
    # Clear any stale entry for this slot (e.g. previous disc's aliases) so get_cached_discs
    # returns only this scan; otherwise both old and new payloads can exist and dedupe by
    # (disc_num, mount_point) may pick the wrong one, and the unfinished job's disc can appear as inserted.
    clear_cached_by_mount_point(mount_point)
    set_cached(mount_point, enriched)
    
    # Update drive list proactively (ensure disc is in the list)
    # Use actual_disc_num (the MakeMKV disc number from disc_info)
    makemkv_disc_num = actual_disc_num
    async with _broadcast_lock:
        current_drives = _normalize_drive_list(_last_drives.get("drives", []))
        disc_exists = any(
            str(d.get("disc_num")) == makemkv_disc_num or d.get("mount_point") == mount_point
            for d in current_drives
        )
        if not disc_exists:
            current_drives.append(build_drive_api_dict(makemkv_disc_num, mount_point))
            _last_drives["drives"] = current_drives
            _last_drives["ts"] = time.time()

            drives_payload = [_drive_payload_for_api(d) for d in current_drives]
            _pending_drive_changes.append(drives_payload)
    
    # Broadcast discinfo update to SSE clients
    cleaned = _clean_discinfo_payload(enriched)
    async with _broadcast_lock:
        # Remove stale entries with the same mount_point but different disc_num
        # This prevents sending incorrect "sr1" events when we have the correct "1" event
        before_count = len(_pending_discinfo_updates)
        _pending_discinfo_updates[:] = [
            update for update in _pending_discinfo_updates
            if not (update.get("mount_point") == mount_point and update.get("disc_num") != actual_disc_num)
        ]
        removed_count = before_count - len(_pending_discinfo_updates)
        _pending_discinfo_updates.append(cleaned)
    
    logger.info(f"Queued disc scan complete notification for disc {actual_disc_num} (hash: {disc_hash})")
    
    # Emit disc_ready and disc_updated to coordinator (scan is complete)
    # Use enriched data which has the most complete information
    disc_id = enriched.get("disc_id") or disc_info.get("disc_id")
    # #723: never announce "ready" for a scan the persist layer just marked
    # failed. The DB and the WebSocket must agree on the same verdict.
    scan_failed = enriched.get("scan_state") == "failed"
    scan_failure_reason = enriched.get("scan_error") or "Disc scan failed"
    if disc_id and scan_failed:
        try:
            from api.routers.websockets import _emit_to_coordinator

            asyncio.create_task(_emit_to_coordinator("disc_scan_failed", {
                "disc_id": disc_id,
                "disc_num": actual_disc_num,
                "mount_point": mount_point,
                "disc_hash": disc_hash,
                "scan_state": "failed",
                "scan_error": scan_failure_reason,
            }))
            logger.warning(
                "Scan persisted as failed for disc %s (disc_num=%s hash=%s): %s",
                disc_id, actual_disc_num, disc_hash, scan_failure_reason,
            )
        except Exception as exc:
            logger.warning(f"Failed to emit disc_scan_failed to websocket: {exc}")
    elif disc_id:
        try:
            from api.routers.websockets import _emit_to_coordinator, _emit_disc_updated_from_info
            # Emit disc_ready message
            asyncio.create_task(_emit_to_coordinator("disc_ready", {
                "disc_id": disc_id,
                "disc_num": actual_disc_num,
                "mount_point": mount_point,
                "disc_hash": disc_hash,
                "scan_state": "ready",
                "scan_error": None,
            }))

            # After enrichment, emit disc_updated with full metadata
            # This ensures cards update when metadata becomes available
            asyncio.create_task(_emit_disc_updated_from_info(enriched, actual_disc_num, mount_point))
            logger.info(f"Emitted WebSocket events for disc {disc_id} (disc_num: {actual_disc_num}, hash: {disc_hash})")
        except Exception as exc:
            logger.warning(f"Failed to emit disc events to websocket: {exc}")
    else:
        logger.error(f"Cannot emit WebSocket events for disc {actual_disc_num}: no disc_id available")

    if disc_id and not mount_point:
        logger.warning(
            "Skipping scan_completed notification: disc_id=%s has empty mount_point (disc_num=%s)",
            disc_id,
            actual_disc_num,
        )

    # In-app toast / optional Discord: drive scan finished; user can start copy from Ripper (#scan_completed)
    if disc_id and mount_point and not scan_failed:
        try:
            from core.job_state import _public_app_base_url
            from core.notifications import emit_notification

            info_title = (
                enriched.get("movie_name")
                or enriched.get("info_title")
                or enriched.get("makemkv_disc_name")
                or enriched.get("disc_name")
            )
            dn = enriched.get("disc_number")
            dn_str = str(dn).strip() if dn is not None and str(dn).strip() else None
            if info_title:
                scan_label = f"{info_title} Disc #{dn_str}" if dn_str else str(info_title)
            elif dn_str:
                scan_label = f"Disc #{dn_str}"
            else:
                scan_label = "Disc"
            title = f"Scan complete: {scan_label}"
            if len(title) > 120:
                title = "Scan complete"
            body = f"Scan finished: {scan_label}. Open MKV Auto to start copying."
            base = _public_app_base_url()
            actions = None
            if base:
                link = f"{base}/ripper"
                body = f"{body} {link}"
                actions = [{"label": "Open MKV-Auto", "url": link}]
            # id_key distinguishes dedupe per content hash so re-scans after eject can notify again
            scan_id_key = str(disc_hash) if disc_hash else None
            await emit_notification(
                body,
                "success",
                "scan_completed",
                action_type="open_ripper_drive",
                action_payload={"mount_point": str(mount_point)},
                # NOT a Job UUID — the disc id doubles as the dedupe identity
                # here. Deep links must not treat it as a job (#841): the
                # drive card auto-selects on load, so plain /activity is the
                # right destination.
                job_id=str(disc_id),
                link_path="/activity",
                title=title,
                actions=actions,
                id_key=scan_id_key,
                info_title=str(info_title) if info_title else None,
            )
        except Exception as exc:
            logger.warning("Failed to emit scan_completed notification: %s", exc)

    # Auto-rip on insert (#331): best-effort, after set_cached() above so the
    # start_rip path sees this scan in the disc-manager cache. start_rip is
    # sync (gatekeeper + Celery dispatch) — run it off the event loop.
    # #723: a failed scan must never auto-start a rip — the disc identity is
    # unreliable, so the rip would be filed under the wrong title.
    if scan_failed:
        logger.info(
            "Skipping auto-rip for mount_point=%s: scan is marked failed (%s)",
            mount_point, scan_failure_reason,
        )
        return
    try:
        from core.auto_rip import maybe_auto_start_rip

        await asyncio.to_thread(maybe_auto_start_rip, enriched)
    except Exception as exc:
        logger.warning("Auto-rip dispatch failed (scan notification unaffected): %s", exc)


def _notify_disc_scan_complete(disc_info: dict) -> None:
    """
    Notify Backend API that disc scan is complete with enriched data.
    Emits final discinfo via SSE.
    
    This function can be called from sync context (Disc Manager).
    Uses asyncio.run_coroutine_threadsafe to bridge to async context.
    
    Args:
        disc_info: Enriched disc information dict (from Disc Manager)
    """
    disc_num = str(disc_info.get("disc_num", ""))
    mount_point = disc_info.get("mount_point", "")
    # Schedule async task
    if _app_ref and hasattr(_app_ref, 'state') and hasattr(_app_ref.state, 'event_loop'):
        loop = _app_ref.state.event_loop
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(_notify_disc_scan_complete_async(disc_info), loop)
        else:
            # Event loop not running, try to schedule directly
            try:
                asyncio.create_task(_notify_disc_scan_complete_async(disc_info))
            except RuntimeError:
                logger.warning("Could not schedule disc scan complete notification: no event loop")
    else:
        logger.warning("Could not schedule disc scan complete notification: no app reference")


def _enrich_payload_with_disc_record(payload: dict, disc_num: str, mount_point: str) -> dict:
    """
    Ensure a disc record exists and inject identifiers/metadata so SSE payloads include disc_id.
    Mirrors the enrichment in api.routers.discs without importing crud there.
    """
    # Skip DB hit only when we already have identifiers and no new scan data to persist.
    has_scan = bool(payload.get("scan_tracks"))
    if payload.get("disc_id") and payload.get("disc_hash") and payload.get("release_id") and not has_scan:
        return payload

    try:
        db = database.SessionLocal()
        disc_rec = crud.ensure_disc_record_from_scan(db, str(disc_num), str(mount_point), payload)
        if disc_rec:
            # #723: the persist layer can downgrade this scan to
            # scan_state='failed' (e.g. empty scan output). Carry that verdict
            # on the payload so the caller emits disc_scan_failed instead of
            # announcing disc_ready with scan_state='ready' — the DB and the
            # WebSocket used to disagree about the very same scan.
            payload["scan_state"] = getattr(disc_rec, "scan_state", None)
            payload["scan_error"] = getattr(disc_rec, "last_scan_error", None)
            payload.setdefault("disc_id", disc_rec.id)
            payload.setdefault("disc_number", disc_rec.disc_number)
            payload.setdefault("discdb_disc_num", getattr(disc_rec, "discdb_disc_num", None))
            payload.setdefault("disc_slug", disc_rec.disc_slug)
            payload.setdefault("disc_name", disc_rec.disc_name)
            payload.setdefault("disc_format", disc_rec.format or payload.get("disc_format") or _infer_disc_format(payload))
            # Include info_title from database if available
            if disc_rec.info_title:
                payload["info_title"] = disc_rec.info_title
            if disc_rec.release:
                release = disc_rec.release
                payload.setdefault("release_id", release.id)
                # Include release metadata for Now Reading card
                payload.setdefault("release_resolution", release.resolution)
                payload.setdefault("release_year", release.release_year)
                payload.setdefault("production_year", release.movie.production_year if release.movie else None)
                # Overwrite empty/null placeholders with real release metadata
                if not payload.get("disc_group"):
                    payload["disc_group"] = release.slug
                else:
                    payload.setdefault("disc_group", release.slug)
                if not payload.get("release_slug"):
                    payload["release_slug"] = release.slug
                else:
                    payload.setdefault("release_slug", release.slug)
                if not payload.get("release_name"):
                    payload["release_name"] = release.name
                else:
                    payload.setdefault("release_name", release.name)
                # Include movie metadata if available
                if release.movie:
                    movie = release.movie
                    payload.setdefault("movie_id", movie.id)
                    payload.setdefault("movie_name", movie.name)
                    payload.setdefault("movie_production_year", movie.production_year)
                    payload.setdefault("movie_tmdb_id", movie.tmdb_id)
                    payload.setdefault("movie_tmdb_type", movie.tmdb_type)
                    payload.setdefault("movie_cover_url", movie.cover_url)
                    payload.setdefault("movie_cover_path", movie.cover_path)
            if is_dev_mode():
                pass
    except Exception as exc:
        logger.warning("Failed to enrich disc payload with DB record in SSE: %s", exc)
    finally:
        try:
            db.close()
        except Exception:
            pass
    return payload


def _hydrate_and_enrich(payload: dict, disc_num: str, mount_point: str) -> dict:
    """
    Normalize a payload (raw or cached) and attach DB identifiers.
    """
    hydrated = payload
    if not payload.get("_hydrated"):
        hydrated = hydrate_disc_payload(
            str(disc_num),
            mount_point,
            {**payload, "disc_num": str(disc_num), "mount_point": mount_point},
        )
        hydrated["_hydrated"] = True
    return _enrich_payload_with_disc_record(hydrated, str(disc_num), str(mount_point))


def _clean_discinfo_payload(payload: dict) -> dict:
    """Strip discinfo to the minimal subset the frontend needs."""
    allowed = {
        "disc_num",
        "makemkv_disc_name",  # MakeMKV disc name from DRV line (e.g., "HARRY_POTTER_SORCERER") - for drive selection
        "disc_name",  # Release metadata disc name (e.g., "Disc 01") - for labeling, user-editable
        "disc_number",  # Release metadata disc number (1, 2, 3...) - for labeling
        "discdb_disc_num",  # TheDiscDB matched disc index (reference only)
        "disc_slug",  # Release metadata disc slug - for labeling, user-editable
        "disc_format",  # Release metadata disc format (Blu-Ray, UHD, DVD) - for labeling
        "mount_point",
        "pending",
        "disc_hash",
        "disc_id",
        "release_id",
        "label_required",
        "label_ready",
        "discdb_miss",
        "discdb_hit",  # Added for DiscDB hit detection
        "movie_name",  # Added for hero card title display (replaces legacy show_title)
        "title_type",  # Added for content type display (Movie/Series)
        "production_year",  # Added for year display
        "release_year",  # Added for year display
        "year",  # Added for year display (fallback)
        "titles",  # Added for title list display
        "tracks",  # Added for DiscDB track mapping
        "db_mapping",  # Added for DiscDB track mapping (legacy)
        "release_image",  # Added for cover image display (replaces legacy show_image)
        "movie_cover_url",  # Added for DiscDB cover image
        "release_resolution",  # Added for resolution display
        "resolution",  # Added for resolution display (disc-level)
        "movie_id",  # Added for TMDB URL visibility
        "movie_name",  # Added for hero card title
        "movie_production_year",  # Added for hero card year
        "movie_tmdb_id",  # Added for TMDB URL population
        "movie_tmdb_type",  # Added for TMDB URL population
        "movie_cover_path",  # Added for hero card cover
        "release_name",  # Added for hero card title
        "release_slug",  # Added for hero card title
        "info_title",  # Added for drive card title display
    }
    cleaned = {k: v for k, v in (payload or {}).items() if k in allowed}
    cleaned.setdefault("disc_num", str(payload.get("disc_num") or ""))
    cleaned.setdefault("mount_point", payload.get("mount_point"))
    cleaned.setdefault("pending", payload.get("pending", False))
    cleaned.setdefault("label_required", payload.get("label_required", False))
    cleaned.setdefault("label_ready", payload.get("label_ready", True))
    # Set discdb_hit if present (can be True, False, or None for pending)
    if "discdb_hit" in payload:
        cleaned["discdb_hit"] = payload.get("discdb_hit")
    # If discdb_hit is not in payload but we have a hash, infer from discdb_miss
    elif payload.get("disc_hash") or payload.get("content_hash"):
        # If discdb_miss is True, then discdb_hit should be False
        if payload.get("discdb_miss"):
            cleaned["discdb_hit"] = False
        # Otherwise, if we have a hash but no discdb_hit/discdb_miss, it's still loading (None)
        else:
            cleaned["discdb_hit"] = None
    # Default to discdb_miss when no hash is present.
    cleaned.setdefault("discdb_miss", bool(payload.get("discdb_miss")) or not bool(payload.get("disc_hash")))
    return cleaned


async def _load_disc_info_async(disc_num: str, mount_point: str, source: str = "async") -> dict:
    """
    Load disc info using Disc Manager, then persist to DB.
    Asynchronously load disc info (hash → discdb → makemkv info) with retry logic.
    Updates cache and broadcasts discinfo event to SSE clients.
    Emits scan state messages to coordinator: disc_scanning → disc_ready/disc_scan_failed
    """
    import json, time, traceback
    from filelock import FileLock, Timeout
    func_logger = get_logger("api.routers.events", "_load_disc_info_async")
    func_logger.debug("_load_disc_info_async called disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    logger.info("Loading disc info async: disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    
    loop = asyncio.get_running_loop()
    
    # Emit disc_scanning message to coordinator
    try:
        from api.routers.websockets import _emit_to_coordinator
        await _emit_to_coordinator("disc_scanning", {
            "disc_id": f"pending-{disc_num}",  # Temporary ID until disc is scanned
            "disc_num": disc_num,
            "mount_point": mount_point,
            "scan_state": "scanning",
        })
        logger.info(f"Emitted disc_scanning for {disc_num} at {mount_point}")
    except Exception as exc:
        logger.warning(f"Failed to emit disc_scanning to coordinator: {exc}")
    
    try:
        # Load disc info via Disc Manager (hash → discdb → makemkv info)
        # Use refresh=True for newly inserted discs to force a scan
        func_logger.debug("About to call get_disc_info with refresh=True disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
        func_logger.debug("About to call get_disc_info in executor disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
        try:
            disc_info = await loop.run_in_executor(
            None, lambda: get_disc_info(str(disc_num), mount_point, refresh=True)
        )
            func_logger.debug("get_disc_info returned successfully disc_num=%s mount_point=%s source=%s has_disc_info=%s disc_hash=%s", 
                            disc_num, mount_point, source, disc_info is not None, disc_info.get("disc_hash") if disc_info else None)
        except Exception as exc:
            func_logger.debug("get_disc_info raised exception disc_num=%s mount_point=%s source=%s error=%s error_type=%s has_status_code=%s status_code=%s", 
                            disc_num, mount_point, source, str(exc), type(exc).__name__, hasattr(exc, "status_code"), getattr(exc, "status_code", None))
            logger.error(f"Failed to load disc info: {exc}")
            raise
        
        # Persist to database using crud module
        disc_hash = disc_info.get("disc_hash")
        if disc_hash:
            try:
                db = database.SessionLocal()
                try:
                    disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, disc_info)
                    crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
                    
                    # Enrich disc_info with database data
                    disc_info["disc_id"] = str(disc_record.id)
                    disc_info["disc_number"] = disc_record.disc_number
                    disc_info["discdb_disc_num"] = getattr(disc_record, "discdb_disc_num", None)
                    disc_info["disc_slug"] = disc_record.disc_slug
                    disc_info["disc_name"] = disc_record.disc_name
                    # Include info_title from database if available (may be more up-to-date than scan data)
                    if disc_record.info_title:
                        disc_info["info_title"] = disc_record.info_title
                    if disc_record.release_id:
                        disc_info["release_id"] = str(disc_record.release_id)
                        if disc_record.release:
                            disc_info["disc_group"] = disc_record.release.slug
                            disc_info["release_name"] = disc_record.release.name
                finally:
                    db.close()
            except Exception as exc:
                logger.warning(f"Failed to persist disc to DB: {exc}")
        
        enriched = _hydrate_and_enrich(
            disc_info,
            str(disc_num),
            str(mount_point)
        )
        # Clear any stale entry for this slot so only this scan is in cache (see _notify_disc_scan_complete_async).
        clear_cached_by_mount_point(mount_point)
        set_cached(mount_point, enriched)
        
        # Broadcast discinfo update to SSE clients
        cleaned = _clean_discinfo_payload(enriched)
        async with _broadcast_lock:
            _pending_discinfo_updates.append(cleaned)
        
        logger.info("Disc info loaded successfully: disc_num=%s hash=%s", disc_num, enriched.get("disc_hash"))
        
        # Emit disc_ready message to coordinator when scan completes successfully
        disc_id = disc_info.get("disc_id")
        disc_hash = enriched.get("disc_hash")
        if disc_id:
            try:
                from api.routers.websockets import _emit_to_coordinator
                await _emit_to_coordinator("disc_ready", {
                    "disc_id": disc_id,
                    "disc_num": disc_num,
                    "mount_point": mount_point,
                    "disc_hash": disc_hash,
                    "scan_state": "ready",
                    "scan_error": None,
                })
                logger.info(f"Emitted disc_ready for {disc_num} (disc_id: {disc_id})")
            except Exception as exc:
                logger.warning(f"Failed to emit disc_ready to coordinator: {exc}")
        
        return enriched
    except DriveManagerError as exc:
        logger.warning("Disc info load failed for %s at %s: %s", disc_num, mount_point, exc)
        err_payload = {
            "type": "disc_error",
            "disc_num": disc_num,
            "mount_point": mount_point,
            "error": str(exc),
        }
        set_cached(mount_point, err_payload)
        async with _broadcast_lock:
            _pending_discinfo_updates.append(err_payload)
        
        # Emit disc_scan_failed message to coordinator
        try:
            from api.routers.websockets import _emit_to_coordinator
            await _emit_to_coordinator("disc_scan_failed", {
                "disc_id": f"pending-{disc_num}",  # Temporary ID since scan failed
                "disc_num": disc_num,
                "mount_point": mount_point,
                "scan_state": "failed",
                "scan_error": str(exc),
            })
            logger.info(f"Emitted disc_scan_failed for {disc_num}")
        except Exception as emit_exc:
            logger.warning(f"Failed to emit disc_scan_failed to coordinator: {emit_exc}")
        
        # Emit user notification for disc read failure (#354)
        try:
            from core.notifications import emit_notification_sync
            emit_notification_sync(
                f"Failed to read disc at {mount_point}. Try reinserting the disc or checking the drive.",
                "error",
                "error_disc_read",
                title="Disc read failed",
            )
        except Exception as notify_exc:
            logger.warning(f"Failed to emit disc read notification: {notify_exc}")
        
        raise
    except Exception as exc:
        logger.warning("Disc info load failed for %s at %s: %s", disc_num, mount_point, exc)
        err_payload = {
            "type": "disc_error",
            "disc_num": disc_num,
            "mount_point": mount_point,
            "error": str(exc),
        }
        set_cached(mount_point, err_payload)
        async with _broadcast_lock:
            _pending_discinfo_updates.append(err_payload)
        
        # Emit disc_scan_failed message to coordinator
        try:
            from api.routers.websockets import _emit_to_coordinator
            await _emit_to_coordinator("disc_scan_failed", {
                "disc_id": f"pending-{disc_num}",  # Temporary ID since scan failed
                "disc_num": disc_num,
                "mount_point": mount_point,
                "scan_state": "failed",
                "scan_error": str(exc),
            })
            logger.info(f"Emitted disc_scan_failed for {disc_num}")
        except Exception as emit_exc:
            logger.warning(f"Failed to emit disc_scan_failed to coordinator: {emit_exc}")
        
        # Emit user notification for disc read failure (#354)
        try:
            from core.notifications import emit_notification_sync
            emit_notification_sync(
                f"Failed to read disc at {mount_point}. Try reinserting the disc or checking the drive.",
                "error",
                "error_disc_read",
                title="Disc read failed",
            )
        except Exception as notify_exc:
            logger.warning(f"Failed to emit disc read notification: {notify_exc}")
        
        raise


async def _trigger_drive_rescan_async(device: str | None = None, change: str | None = None, source: str = "udev"):
    """
    Asynchronously trigger drive rescan and disc info loading.
    Updates drive cache, then loads disc info for new discs.
    Handles both udev triggers and manual frontend requests.
    """
    import json, time, traceback
    func_logger = get_logger("api.routers.events", "_trigger_drive_rescan_async")
    func_logger.debug("_trigger_drive_rescan_async called device=%s change=%s source=%s", device, change, source)
    logger.info("Triggering async drive rescan: device=%s change=%s source=%s", device, change, source)
    loop = asyncio.get_running_loop()
    
    # Clear drive cache to force rescan
    _drive_scan_event.clear()

    # Update drive list
    try:
        # For udev_insert events, skip list_drives call - drive list is updated proactively
        # by _notify_disc_inserted and _notify_disc_scan_complete_async
        if source == "udev_insert":
            # Just broadcast any pending drive changes (already queued by proactive updates)
            # Drive list update was already queued by _notify_disc_inserted, but we still need to
            # set the scan event so the SSE loop can continue (it's waiting for the event)
            _drive_scan_event.set()
            return
        
        # For udev_eject events, skip MakeMKV scan - just remove drive from cache
        # This prevents MakeMKV from probing the drive which causes physical reinsertion on USB drives
        if source == "udev_eject" and device:
            raw_drives = _normalize_drive_list(_last_drives.get("drives", []))
            raw_drives = [d for d in raw_drives if d.get("mount_point") != device]
            _last_drives["drives"] = raw_drives

            drives_payload = [_drive_payload_for_api(d) for d in raw_drives]
            async with _broadcast_lock:
                _pending_drive_changes.append(drives_payload)

            _drive_scan_event.set()

            logger.info("Eject handled without rescan: removed device=%s from drive list", device)
            return  # Skip MakeMKV scan entirely
        
        # Always scan all drives to get current state
        raw_drives = await _load_drive_list(loop, force=True)
        
        if device:
            # Single drive rescan - extract disc_num from device path
            m = re.search(r"sr(\d+)$", device)
            disc_num_hint = m.group(1) if m else None
            if disc_num_hint:
                target_drive = None
                for d in raw_drives:
                    num = str(d.get("disc_num", ""))
                    mp = str(d.get("mount_point", ""))
                    if num == disc_num_hint or mp == device:
                        target_drive = (num, mp)
                        break

                if target_drive:
                    disc_num, mount_point = target_drive
                    # For udev_insert events, skip disc info loading (handle_disc_insert handles it proactively)
                    # For udev_eject events, skip disc info loading (disc was just ejected, no disc to scan)
                    if source not in ("udev_insert", "udev_eject"):
                        # Clear stale cache for this drive so coordinator shows scanning until new disc is loaded
                        clear_cached_key(disc_num)
                        # Check if already loading
                        async with _disc_load_lock:
                            if disc_num not in _active_disc_loads:
                                # Emit pending placeholder
                                pending = {
                                    "disc_num": disc_num,
                                    "mount_point": mount_point,
                                    "pending": True,
                                    "label_required": False,
                                    "label_ready": True,
                                    "discdb_hit": None,  # None indicates DiscDB lookup is in progress
                                    "discdb_miss": True,
                                }
                                set_cached(mount_point, pending)
                                async with _broadcast_lock:
                                    _pending_discinfo_updates.append(pending)
                                
                                # Start disc info loading task
                                task = asyncio.create_task(_load_disc_info_async(disc_num, mount_point, source))
                                _active_disc_loads[disc_num] = task
                                
                                def cleanup_task(disc_num: str, task: asyncio.Task):
                                    async def _cleanup():
                                        try:
                                            await task
                                        except Exception:
                                            pass
                                        finally:
                                            async with _disc_load_lock:
                                                _active_disc_loads.pop(disc_num, None)
                                    asyncio.create_task(_cleanup())
                                
                                cleanup_task(disc_num, task)
                    elif source == "udev_insert":
                        logger.info("Skipping disc info loading for udev_insert (handled proactively by handle_disc_insert)")
                    elif source == "udev_eject":
                        logger.info("Skipping disc info loading for udev_eject (disc was just ejected, no disc to scan)")
                else:
                    logger.warning("Drive not found in scan: device=%s disc_num_hint=%s", device, disc_num_hint)
        
        # Update drive list cache
        raw_drives = _normalize_drive_list(raw_drives)
        _last_drives["drives"] = raw_drives
        drives_payload = [_drive_payload_for_api(d) for d in raw_drives]
        
        # Broadcast drive change event
        async with _broadcast_lock:
            _pending_drive_changes.append(drives_payload)
        
        # Load disc info for all drives (if not already loading)
        if not device:  # Only for full rescans
            for d in raw_drives:
                disc_num = str(d.get("disc_num", ""))
                mount_point = str(d.get("mount_point", ""))
                async with _disc_load_lock:
                    if disc_num not in _active_disc_loads:
                        # Emit pending placeholder
                        pending = {
                            "disc_num": str(disc_num),
                            "mount_point": mount_point,
                            "pending": True,
                            "label_required": False,
                            "label_ready": True,
                            "discdb_hit": None,  # None indicates DiscDB lookup is in progress
                            "discdb_miss": True,
                        }
                        set_cached(mount_point or disc_num, pending)
                        async with _broadcast_lock:
                            _pending_discinfo_updates.append(pending)
                        
                        # Start disc info loading task
                        task = asyncio.create_task(_load_disc_info_async(str(disc_num), mount_point, source))
                        _active_disc_loads[str(disc_num)] = task
                        
                        def cleanup_task(disc_num: str, task: asyncio.Task):
                            async def _cleanup():
                                try:
                                    await task
                                except Exception:
                                    pass
                                finally:
                                    async with _disc_load_lock:
                                        _active_disc_loads.pop(disc_num, None)
                            asyncio.create_task(_cleanup())
                        
                        cleanup_task(str(disc_num), task)
        
        # Mark scan as complete
        _drive_scan_event.set()
        
        logger.info("Async drive rescan triggered: drives=%s", drives_payload)
    except Exception as exc:
        logger.error("Failed to trigger async drive rescan: %s", exc)
        _drive_scan_event.set()
        raise


async def _load_drive_list(loop: asyncio.AbstractEventLoop, force: bool = False):
    """
    Load the drive list in a background thread so we never block the event loop.
    Cached results are reused until explicitly refreshed (service restart or explicit refresh).
    """
    if _last_drives["drives"] and not force:
        return _normalize_drive_list(_last_drives["drives"])

    async with _drive_scan_lock:
        if _last_drives["drives"] and not force:
            return _normalize_drive_list(_last_drives["drives"])
        # cross-process guard so concurrent uvicorn workers/tabs don't spawn multiple scans
        try:
            lock_timeout = DRIVE_SCAN_TIMEOUT if DRIVE_SCAN_TIMEOUT > 0 else -1
            with FileLock(DRIVE_SCAN_FILELOCK, timeout=lock_timeout):
                if _last_drives["drives"] and not force:
                    return _normalize_drive_list(_last_drives["drives"])
                logger.debug("scanning drives via makemkvcon…")
                logger.info("Drive scan started (force=%s)", force)
                try:
                    raw_drives = await loop.run_in_executor(None, list_drives)
                    raw_drives = _normalize_drive_list(raw_drives)
                except asyncio.TimeoutError as exc:
                    raise RuntimeError(f"Drive scan timed out after {DRIVE_SCAN_TIMEOUT}s") from exc
                except Exception as exc:
                    if isinstance(exc, DriveManagerError):
                        raise RuntimeError(f"Drive scan failed: {exc}") from exc
                    raise
                _last_drives["drives"] = raw_drives
                logger.debug("drive scan complete: %s", raw_drives)
                logger.info("Drive scan complete: %s", raw_drives)
                _drive_scan_event.set()
                return raw_drives
        except Timeout as exc:
            raise RuntimeError("Drive scan already in progress") from exc

# REMOVED: /events/drive SSE endpoint - replaced by Workflow Coordinator WebSocket
# The coordinator now provides all disc metadata (inserted + unfinished) via WebSocket
# This eliminates data duplication and provides a single source of truth
# All disc metadata now comes from /ws/workflow-coordinator


@router.post("/drive/rescan")
async def rescan_drives(request: Request):
    """
    Force a drive rescan (makemkvcon) even if autoscan is disabled.
    Stream SSE events so the UI stays in a loading state until discinfo completes.
    """
    import time
    import json
    import traceback
    request_start = time.time()
    logger.info("rescan_drives endpoint called from %s", request.client.host if request.client else "unknown")

    # Multi-drive identity-swap detection (#540 runtime complement to PR #550's
    # API gate). Every udev event is a chance for the kernel to have silently
    # reassigned a /dev/srN to a different physical drive. If we detect that,
    # fail any active rip job whose drive_by_id_serial no longer matches.
    try:
        from core.drive_swap_handler import check_and_handle_swaps
        db_for_swap = database.SessionLocal()
        try:
            check_and_handle_swaps(db_for_swap)
        finally:
            db_for_swap.close()
    except Exception as swap_exc:
        logger.warning("drive_swap_handler check failed (continuing): %s", swap_exc)
    
    wants_stream = "text/event-stream" in (request.headers.get("accept") or "")
    stream_param = request.query_params.get("stream")
    if stream_param and stream_param.lower() in ("0", "false", "no"):
        wants_stream = False

    loop = asyncio.get_running_loop()
    # clear cache so next scan runs
    _last_drives["drives"] = []
    _drive_scan_event.clear()
    
    # Device hint: query > JSON body (frontend refresh) > form (udev script). Read body only once.
    form_device = None
    form_change = None
    device_hint: str | None = request.query_params.get("device") or request.query_params.get("dev")
    ct = (request.headers.get("content-type") or "").lower()
    if not device_hint and "application/json" in ct:
        try:
            body = await request.json()
            device_hint = body.get("device") or body.get("dev")
            if isinstance(device_hint, str):
                device_hint = device_hint.strip() or None
        except Exception:
            pass
    if "application/x-www-form-urlencoded" in ct or "multipart/form-data" in ct:
        try:
            form = await request.form()
            form_device = form.get("device")
            form_change = form.get("change")
            if isinstance(form_device, str):
                form_device = form_device.strip() or None
            if isinstance(form_change, str):
                form_change = form_change.strip() or None
        except Exception:
            pass
    if not device_hint:
        device_hint = form_device

    # Non-streaming mode: trigger rescan asynchronously and return immediately.
    if not wants_stream:
        response_start = time.time()
        try:
            
            # Trigger async rescan - this will update drive cache and load disc info in background
            # Source indicates where the request came from
            source = "manual" if not form_device else "udev"
            asyncio.create_task(_trigger_drive_rescan_async(device_hint, form_change, source))
            
            # Return immediately with current drives list (will be updated by background task)
            raw_drives = _normalize_drive_list(_last_drives.get("drives", []))
            drives = [_drive_payload_for_api(d) for d in raw_drives]
            
            response_time = time.time() - response_start
            total_time = time.time() - request_start
            
            logger.info("Rescan triggered async (non-streaming): device=%s source=%s, returning current drives=%s (total_time=%.3fs)", device_hint, source, drives, total_time)
            return JSONResponse(drives)
        except Exception as exc:
            error_time = time.time() - request_start
            logger.error("Failed to trigger async rescan: %s", exc)
            raise HTTPException(503, detail=str(exc)) from exc

    async def event_stream():
        # Tell UI we're scanning so it stays in a loading state until discinfo finishes.
        yield "event: drive_status\n"
        yield f"data: {json.dumps({'state': 'scanning'})}\n\n"

        # device_hint and form_device already set above (query > JSON body > form)
        # Source indicates where the request came from
        source = "manual" if not form_device else "udev"
        
        # Trigger async rescan - this will update drive cache and load disc info
        try:
            await _trigger_drive_rescan_async(device_hint, form_change, source)
        except Exception as exc:
            logger.error("Failed to trigger async rescan in streaming mode: %s", exc)
            yield "event: error\n"
            yield f"data: {json.dumps({'type': 'rescan_error', 'message': str(exc)})}\n\n"
            yield "event: drive_status\n"
            yield f"data: {json.dumps({'state': 'error', 'message': str(exc)})}\n\n"
            return
        
        # Wait for drive scan to complete
        try:
            await asyncio.wait_for(_drive_scan_event.wait(), timeout=DRIVE_SCAN_TIMEOUT if DRIVE_SCAN_TIMEOUT > 0 else 60)
        except asyncio.TimeoutError:
            logger.warning("Drive scan wait timed out in streaming rescan")
        
        # Emit drive list
        norm = _normalize_drive_list(_last_drives.get("drives", []))
        drives_payload = [_drive_payload_for_api(d) for d in norm]
        yield "event: drive\n"
        yield f"data: {json.dumps(drives_payload)}\n\n"
        
        # Emit pending placeholders for all drives
        for d in norm:
            disc_num = str(d.get("disc_num", ""))
            mount = str(d.get("mount_point", ""))
            cached = get_cached(mount) or get_cached(disc_num)
            if cached and not cached.get("pending"):
                # Already have full info, emit it
                payload = _hydrate_and_enrich(cached, str(disc_num), mount)
                set_cached(mount or disc_num, payload)
                cleaned = _clean_discinfo_payload(payload)
                yield "event: discinfo\n"
                yield f"data: {json.dumps(cleaned)}\n\n"
            else:
                # Emit pending placeholder
                pending = {
                    "disc_num": str(disc_num),
                    "mount_point": mount,
                    "pending": True,
                    "label_required": False,
                    "label_ready": True,
                    "discdb_miss": True,
                }
                yield "event: discinfo\n"
                yield f"data: {json.dumps(pending)}\n\n"
        
        # Wait a bit for disc info to load, then emit updates
        # The async tasks will broadcast via _pending_discinfo_updates
        await asyncio.sleep(1)
        
        # Check for any discinfo updates that completed quickly
        async with _broadcast_lock:
            if _pending_discinfo_updates:
                for discinfo_update in _pending_discinfo_updates:
                    if discinfo_update.get("type") == "disc_error":
                        yield "event: error\n"
                        yield f"data: {json.dumps({'type': 'disc_error', **discinfo_update})}\n\n"
                    else:
                        yield "event: discinfo\n"
                        yield f"data: {json.dumps(discinfo_update)}\n\n"
                _pending_discinfo_updates.clear()
        
        yield "event: drive_status\n"
        yield f"data: {json.dumps({'state': 'ready'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/previews/queue", response_class=StreamingResponse)
async def preview_queue_stream(request: Request, interval: float = 5.0):
    """
    SSE stream for preview queue status so the frontend doesn't have to poll.
    """
    async def event_source():
        while not await request.is_disconnected():
            try:
                db = database.SessionLocal()
                try:
                    payload = preview_queue_snapshot(db)
                finally:
                    db.close()
                yield "event: queue\n"
                yield f"data: {json.dumps(payload, default=str)}\n\n"
            except Exception as exc:
                logger.warning("Preview queue stream error: %s", exc)
                yield "event: error\n"
                yield f"data: {json.dumps({'message': str(exc)}, default=str)}\n\n"
            await asyncio.sleep(max(interval, 1.0))

    return StreamingResponse(event_source(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/job/{job_id}", response_class=StreamingResponse)
async def job_events(job_id: str, request: Request):
    """
    SSE endpoint that streams:
      - 'progress' events with {jobId, status, progress, logs, result_location}
    """
    # validate existence before starting stream
    initial = crud.get_job(database.SessionLocal(), job_id)
    if not initial:
        raise HTTPException(404, detail="Job not found")

    async def event_source():
        logger.debug("client connected for /events/job/%s", job_id)
        while not await request.is_disconnected():
            db = database.SessionLocal()
            try:
                job = crud.get_job(db, job_id)
                if not job:
                    yield "event: error\n"
                    yield f"data: {json.dumps({'type': 'job_missing', 'jobId': job_id})}\n\n"
                    break

                pipeline, phase = _derive_pipeline(job)
                per_title_progress = getattr(job, "per_title_progress", None) or {}
                completed_titles = (job.disc_payload or {}).get("completed_titles") if job.disc_payload else None
                skipped_titles = (job.disc_payload or {}).get("skipped_titles") if job.disc_payload else None
                per_title_status = None
                if per_title_progress:
                    per_title_status = {}
                    for k, v in per_title_progress.items():
                        try:
                            pct = int(v)
                        except Exception:
                            pct = 0
                        if skipped_titles and k in skipped_titles:
                            per_title_status[k] = "skipped"
                        elif pct >= 100:
                            per_title_status[k] = "completed"
                        elif pct > 0:
                            per_title_status[k] = "running"
                        else:
                            per_title_status[k] = "pending"
                    if completed_titles:
                        for k in completed_titles:
                            per_title_status[k] = "completed"
                    if skipped_titles:
                        for k in skipped_titles:
                            per_title_status[k] = "skipped"

                disc_payload = job.disc_payload or {}
                label_required = bool(disc_payload.get("label_required"))
                label_ready = bool(disc_payload.get("label_ready"))
                disc_hash = None
                try:
                    disc_hash = getattr(job.disc, "content_hash", None)
                except Exception:
                    pass
                if not disc_hash:
                    disc_hash = disc_payload.get("disc_hash")
                status = {
                    "jobId":           str(job.id),
                    "job_status":      job.job_status,
                    "rip_progress":    job.rip_progress,
                    "rip_phase":       getattr(job, "rip_phase", None),
                    "logs":            job.logs or [],
                    "job_dir": str(JobPaths.for_id(str(job.id)).root),
                    "transfer_status": getattr(job, "transfer_status", None),
                    "transfer_progress": getattr(job, "transfer_progress", None),
                    "transfer_error": getattr(job, "transfer_error", None),
                    "titlesCompleted": getattr(job, "titles_completed", None),
                    "totalTitles": getattr(job, "total_titles", None),
                    "currentTitleProgress": getattr(job, "current_title_progress", None),
                    "currentTitleId": getattr(job, "current_title_id", None),
                    "currentTitleNumber": getattr(job, "current_title_number", None),
                    "perTitleProgress": per_title_progress or None,
                    "disc_hash":       disc_hash,
                    "perTitleStatus": per_title_status,
                    "completedTitles": completed_titles,
                    "skippedTitles": skipped_titles,
                    "disc_payload": {k: v for k, v in disc_payload.items() if k not in ("label_payload", "label_draft")} or None,
                    "label_draft": None,
                    "label_required": label_required,
                    "label_ready": label_ready,
                    "label_state": pipeline.get("label"),
                    "finalize_state": getattr(job, "finalize_state", None),
                    "finalize_release_state": getattr(job, "finalize_release_state", None),
                    "stage_profile": getattr(job, "stage_profile", None),
                    "discdb_result": getattr(job, "discdb_result", None),
                    "pipeline": pipeline,
                    "phase": phase,
                    "dev_mode": getattr(job, "dev_mode", None),
                    "dev_validation": getattr(job, "dev_validation", None),
                    "export_path": getattr(job, "export_path", None),
                }
                yield "event: progress\n"
                yield f"data: {json.dumps(status)}\n\n"
            finally:
                db.close()

            await asyncio.sleep(1)

        logger.debug("/events/job/%s client disconnected", job_id)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/drive/eject")
async def drive_eject(request: Request):
    """
    Handle drive ejection events from udev: clear cached disc info and invalidate drive list.
    Triggers a rescan to notify connected SSE clients of the ejection.
    """
    # Multi-drive identity-swap detection (#540). See rescan_drives for the
    # full rationale — eject events are particularly important because the
    # USB bus reset that follows tray-open is the canonical trigger for the
    # kernel-renumbering catastrophe surfaced in the 2026-06 diagnostic.
    try:
        from core.drive_swap_handler import check_and_handle_swaps
        db_for_swap = database.SessionLocal()
        try:
            check_and_handle_swaps(db_for_swap)
        finally:
            db_for_swap.close()
    except Exception as swap_exc:
        logger.warning("drive_swap_handler check failed (continuing): %s", swap_exc)

    try:
        form = await request.form()
    except Exception:
        form = {}
    device = form.get("device") if isinstance(form, dict) else None
    change = form.get("change") if isinstance(form, dict) else None
    disc_num = None
    if device and isinstance(device, str):
        m = re.search(r"sr(\d+)$", device)
        if m:
            disc_num = m.group(1)

    # Resolve cached payload by device so we clear the right entry (cache may be keyed by "0"
    # while udev sends device /dev/sr1 -> regex gives "1"; persisted cache survives restarts)
    cached = get_cached_by_mount_point(device) if device else None
    if not cached and disc_num:
        cached = get_cached(str(disc_num))
    if cached and disc_num is None:
        disc_num = cached.get("disc_num")
    disc_id = cached.get("disc_id") if cached else None
    disc_hash = (cached.get("disc_hash") or cached.get("content_hash")) if cached else None

    if device:
        # Kill any makemkvcon running for this drive so it doesn't outlive the eject
        kill_disc_num = str(disc_num) if disc_num else device
        try:
            from core.utils import kill_makemkvcon_for_disc
            if kill_makemkvcon_for_disc(kill_disc_num):
                logger.info("Killed makemkvcon for drive %s (eject)", kill_disc_num)
        except Exception as kill_exc:
            logger.warning("Failed to kill makemkvcon for drive %s on eject: %s", kill_disc_num, kill_exc)

        # Clear cache by device path so the correct key is cleared (and persisted); avoids
        # disc_num mismatch (e.g. udev "1" vs app cache "0") and restarts re-showing the disc
        from core.utils import _find_makemkvcon_process_for_disc
        pid, _ = _find_makemkvcon_process_for_disc(kill_disc_num)
        if pid:
            logger.warning("Skipping cache clear for device %s: makemkvcon still running (PID %s)", device, pid)
        else:
            cleared = clear_cached_by_mount_point(device)
            if cleared:
                logger.debug("ejected disc from device %s", device)
                logger.info("Disc ejected: device=%s (cleared by mount_point)", device)
            elif disc_num:
                clear_cached_key(str(disc_num))
                logger.debug("ejected disc %s from device %s", disc_num, device)
                logger.info("Disc ejected: disc_num=%s, device=%s", disc_num, device)

    if device:
        # Fail running/pending jobs for this disc and revoke their Celery tasks
        try:
            from api.routers.jobs import _fail_jobs_for_disc
            db = database.SessionLocal()
            try:
                if disc_hash:
                    failed_job_ids = _fail_jobs_for_disc(
                        disc_hash=disc_hash, db=db, reason="disc ejected"
                    )
                else:
                    failed_job_ids = _fail_jobs_for_disc(
                        db=db, reason="disc ejected", mount_point=device
                    )
                if failed_job_ids:
                    logger.info(
                        "Marked %d job(s) as failed due to disc ejection: %s",
                        len(failed_job_ids),
                        failed_job_ids,
                    )
            finally:
                db.close()
        except Exception as job_fail_exc:
            logger.warning("Failed to mark jobs as failed after disc ejection: %s", job_fail_exc)

        # Also log to drive_manager log for visibility
        drive_manager_logger = logging.getLogger("drive_manager")
        drive_manager_logger.info("UDEV eject triggered: disc_num=%s, device=%s, change=%s", disc_num, device, change)

        # Broadcast ejection event to all connected SSE clients
        eject_event = {
            "disc_num": str(disc_num) if disc_num else "",
            "device": device,
            "event_type": "disc_eject"
        }
        async with _ejection_lock:
            _pending_ejections.append(eject_event)

        # Emit to master websocket (disc ejected)
        # Always emit, even if disc_id is missing - frontend can match by disc_num/mount_point
        try:
            from api.routers.websockets import _emit_to_coordinator
            asyncio.create_task(_emit_to_coordinator("disc_ejected", {
                "disc_id": disc_id,  # May be None
                "disc_num": str(disc_num) if disc_num else "",
                "mount_point": device,
                "disc_hash": disc_hash,
            }))
            logger.info("Emitted disc_ejected for disc_num=%s, disc_id=%s, mount_point=%s", disc_num, disc_id, device)
        except Exception as exc:
            logger.warning("Failed to emit disc_ejected to websocket: %s", exc)

    # reset cached drives so next poll re-scans
    _last_drives["drives"] = []
    _drive_scan_event.clear()
    
    # Trigger async rescan to notify SSE clients
    # The rescan will update the drive list without probing MakeMKV (to avoid physical reinsertion)
    loop = asyncio.get_running_loop()
    try:
        # Trigger rescan in background without blocking - use udev_eject source (skips MakeMKV probe)
        asyncio.create_task(_trigger_drive_rescan_async(device, change, "udev_eject"))
        logger.info("Eject handled: device=%s disc_num=%s (skipped MakeMKV rescan)", device, disc_num)
    except Exception as exc:
        logger.debug("failed to trigger rescan after eject: %s", exc)
        logger.warning("Failed to trigger rescan after eject: %s", exc)
    
    return {"status": "ok", "device": device, "disc_num": disc_num}


@router.get("/makemkv/{job_id}", response_class=StreamingResponse, deprecated=True)
async def makemkv_update_events(job_id: str, request: Request):
    """
    [DEPRECATED] Stream MakeMKV update job logs and status via Server-Sent Events.
    
    This endpoint is deprecated and will be removed in a future version.
    Please use the unified WebSocket at /ws/workflow for real-time updates
    and GET /system/makemkv/update/job/{job_id} for HTTP polling fallback.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    async def event_source():
        import time
        connection_start = time.time()
        keepalive_count = 0
        message_count = 0
        logger.debug("[SSE] client connected for /events/makemkv/%s", job_id)
        last_keepalive = time.time()
        try:
            while not await request.is_disconnected():
                try:
                    # Wait for queue item with 5s timeout
                    kind, payload = await asyncio.wait_for(job.queue.get(), timeout=5.0)
                    if kind == "log":
                        yield "event: log\n"
                        yield f"data: {json.dumps({'line': payload})}\n\n"
                        # Don't update last_keepalive here - let pings happen on schedule
                        message_count += 1
                        if message_count % 10 == 0:  # Log every 10th message
                            logger.debug("[SSE] %s messages sent (connection age: %.1fs)", message_count, time.time() - connection_start)
                    elif kind == "status":
                        yield "event: status\n"
                        yield f"data: {json.dumps(payload)}\n\n"
                        # Don't update last_keepalive here - let pings happen on schedule
                        message_count += 1
                    elif kind == "done":
                        break
                except asyncio.TimeoutError:
                    pass  # Queue empty, check keepalive below
                except asyncio.CancelledError:
                    logger.debug("[SSE] /events/makemkv/%s cancelled during yield", job_id)
                    raise
                
                # Send keepalive every 5s regardless of queue activity (changed from 10s)
                if time.time() - last_keepalive >= 5:
                    # Use real 'ping' event instead of SSE comment
                    yield "event: ping\n"
                    yield "data: {}\n\n"
                    last_keepalive = time.time()
                    keepalive_count += 1
                    logger.debug("[SSE] keepalive #%s sent (connection age: %.1fs)", keepalive_count, time.time() - connection_start)
                
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            logger.debug("[SSE] /events/makemkv/%s connection cancelled", job_id)
            raise
        except Exception as e:
            logger.debug("[SSE] /events/makemkv/%s error: %s", job_id, e)
            raise
        finally:
            duration = time.time() - connection_start
            logger.debug("[SSE] client disconnected after %.1fs (%s messages, %s keepalives)", duration, message_count, keepalive_count)

    return StreamingResponse(
        event_source(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "Keep-Alive": "timeout=600",
        },
    )
