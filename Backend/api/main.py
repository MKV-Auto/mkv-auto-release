# main.py

import asyncio
import logging
import threading
import json
import time
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
from contextlib import suppress
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Depends

from api.routers import jobs, events, system, discs, discdb, releases, movies, drives, websockets
from sqlalchemy import text
from api import database, crud
from api import models as db_models
from workers.tasks import gather_final_outputs
from core.loop_local import LoopLocalLock
from core.utils import get_mkvauto_tmp, get_mkvauto_root
from core.job_state import apply_job_state
from core.logging_utils import get_logger, _get_log_level_from_env
from core.log_file_config import LOG_ROTATE_BACKUP_COUNT, LOG_ROTATE_MAX_BYTES
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Body

try:
    from drive_manager.uds_server import UDSServer  # type: ignore
except Exception:
    UDSServer = None  # type: ignore

def _configure_logging():
    """
    Attach rotating file handlers so backend logs are persisted.
    Uses centralized logging utility to respect MKVAUTO_DEBUG_LEVEL.
    
    Separates HTTP/uvicorn logs from API/backend logs:
    - uvicorn.log: HTTP access/error logs from uvicorn
    - api.log: Backend API, core modules, and transfer logs
    """
    # Use MKVAUTO_ROOT/logs instead of Backend/logs to match other log files
    logs_dir = get_mkvauto_root() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    
    # Get log level from environment
    log_level = _get_log_level_from_env()
    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    
    # HTTP/uvicorn log file (HTTP access and error logs)
    uvicorn_handler = RotatingFileHandler(
        logs_dir / "uvicorn.log", maxBytes=LOG_ROTATE_MAX_BYTES, backupCount=LOG_ROTATE_BACKUP_COUNT
    )
    uvicorn_handler.setFormatter(formatter)
    uvicorn_handler.setLevel(log_level)
    
    # Backend API log file (API routes, core modules, and transfer operations)
    api_handler = RotatingFileHandler(
        logs_dir / "api.log", maxBytes=LOG_ROTATE_MAX_BYTES, backupCount=LOG_ROTATE_BACKUP_COUNT
    )
    api_handler.setFormatter(formatter)
    api_handler.setLevel(log_level)
    
    # HTTP/uvicorn loggers → uvicorn.log
    # Remove all existing handlers (including console/stream handlers) to prevent duplication
    # All uvicorn output will go through the file handler only (no console output after logging is configured)
    # Note: stderr redirect in manage.sh captures startup errors before logging is configured
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        # Remove all existing handlers (console, stream, file) to prevent duplication
        logger.handlers = []
        logger.addHandler(uvicorn_handler)
        # Prevent propagation to root logger to avoid console output and duplication
        logger.propagate = False

    # Backend API loggers → api.log (includes transfer logs)
    # Python logging hierarchy: "api.routers.jobs" inherits from "api", "core.drive_gatekeeper" inherits from "core"
    # By configuring parent loggers, all child loggers automatically inherit the handlers
    for name in ("api", "core", "transfer"):
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        # Remove any existing RotatingFileHandlers to avoid duplicates
        logger.handlers = [h for h in logger.handlers if not isinstance(h, RotatingFileHandler)]
        logger.addHandler(api_handler)
        # ensure console/file handlers carry timestamps too
        for h in logger.handlers:
            try:
                h.setFormatter(formatter)
                h.setLevel(log_level)
            except Exception:
                pass
    
    # Also configure core.utils.parse specifically (though it should inherit from "core")
    parse_logger = logging.getLogger("core.utils.parse")
    parse_logger.setLevel(log_level)
    parse_logger.handlers = [h for h in parse_logger.handlers if not isinstance(h, RotatingFileHandler)]
    parse_logger.addHandler(api_handler)
    for h in parse_logger.handlers:
        try:
            h.setFormatter(formatter)
            h.setLevel(log_level)
        except Exception:
            pass

_configure_logging()

# Global UDS server instance
_uds_server: Optional[object] = None
# Global reference to app for scheduling async tasks
_app_instance: Optional[FastAPI] = None


# Track recent ejections to prevent spurious reinsertion detection
# Maps mount_point (device path) -> timestamp of ejection
# Keyed by mount_point (stable physical identity), not disc_num (volatile MakeMKV index).
_recent_ejections: dict[str, float] = {}
_EJECT_COOLDOWN_SECONDS = 5.0  # Ignore insert events within this window after eject

# Track recent insertions to prevent spurious eject detection from duplicate udev events
# Maps mount_point (device path) -> timestamp of insertion
_recent_insertions: dict[str, float] = {}
_INSERT_STABILIZATION_SECONDS = 5.0  # Don't treat as eject within this window after insert


def _handle_udev_event(action: str, device: str, disc_num: Optional[str] = None) -> dict:
    """
    Handle udev event (insert/eject/change) from UDS server.
    Calls drive operations directly (no HTTP).

    For 'change' action (DISK_MEDIA_CHANGE=1), uses physical presence checks:
    - Media readable -> treated as INSERT (legacy udev often only sends "change").
    - Not readable -> EJECT.

    A 'change' that fires while the slot is already *stable* (completed scan, no eject since)
    may be SCSI/media noise: we skip a full rescan when the drive is not busy and a quick
    content hash matches the cached disc_hash. Rip/hash/info in progress suppresses even
    the hash probe to avoid thrashing the drive.
    """
    logger = get_logger(__name__, "_handle_udev_event")
    logger.info(f"Received udev event: action={action}, device={device}, disc_num={disc_num}")
    global _app_instance

    # #562 PR 4: any add/change/remove makes the drive_registry's media
    # snapshot stale by definition — invalidate so the next caller (UI tick,
    # rip-start gate, etc.) re-reads from sg_turs/udevadm instead of serving
    # the pre-event cached state.
    try:
        from core.drive_registry import invalidate as _registry_invalidate

        _registry_invalidate()
    except Exception as exc:
        logger.debug("drive_registry invalidate failed (non-fatal): %s", exc)

    try:
        from core._drive_operations import handle_disc_eject, handle_disc_insert, handle_disc_eject_for_device
        from core.disc_cache import get as cache_get
        import time

        raw_udev_action = action

        # Physical state detection for 'change' events
        # Don't rely on cache state alone - check if disc is actually physically present
        if action == "change":
            # Check physical disc presence by testing device accessibility
            # A readable block device with media indicates disc is present
            import os
            import stat
            
            disc_physically_present = False
            detection_method = "none"
            detection_details = {}
            
            try:
                if os.path.exists(device):
                    st = os.stat(device)
                    if stat.S_ISBLK(st.st_mode):
                        # Try multiple detection methods (optical drives can be tricky)
                        import subprocess
                        
                        # Method 1: Direct device read test (most reliable - tests actual accessibility)
                        # Try to open and read from the device - if it fails, no disc is present
                        try:
                            # Open device in read-only mode with non-blocking flag
                            # This will fail immediately if no media is present
                            fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
                            try:
                                # Try to read a single byte - if media present, this succeeds
                                # If no media, this raises OSError
                                os.read(fd, 1)
                                disc_physically_present = True
                                detection_method = "device_read"
                                detection_details["device_readable"] = True
                            except OSError as e:
                                # Read failed - likely no media
                                detection_details["device_read_error"] = str(e)
                                detection_details["device_readable"] = False
                            finally:
                                os.close(fd)
                        except OSError as e:
                            # Open failed - definitely no media or device issue
                            detection_details["device_open_error"] = str(e)
                            detection_details["device_openable"] = False
                        
                        # Method 2: Use blkid (more reliable than blockdev for optical)
                        if not disc_physically_present:
                            try:
                                result = subprocess.run(
                                    ["blkid", device],
                                    capture_output=True,
                                    text=True,
                                    timeout=1.0
                                )
                                # blkid returns 0 if media present, 2 if no media
                                if result.returncode == 0:
                                    disc_physically_present = True
                                    detection_method = "blkid"
                                    detection_details["blkid_output"] = result.stdout.strip()[:100]
                                detection_details["blkid_returncode"] = result.returncode
                            except Exception as e:
                                detection_details["blkid_error"] = str(e)
                        
                        # Method 3: blockdev --getsize (backup method)
                        if not disc_physically_present:
                            try:
                                result = subprocess.run(
                                    ["blockdev", "--getsize", device],
                                    capture_output=True,
                                    text=True,
                                    timeout=1.0
                                )
                                if result.returncode == 0:
                                    size = int(result.stdout.strip())
                                    if size > 0:
                                        disc_physically_present = True
                                        detection_method = "blockdev"
                                        detection_details["blockdev_size"] = size
                                detection_details["blockdev_returncode"] = result.returncode
                            except Exception as e:
                                detection_details["blockdev_error"] = str(e)
            except Exception as e:
                logger.debug(f"Physical disc detection failed for {device}: {e}")
                detection_details["detection_error"] = str(e)
            
            # Determine action based on physical presence, not cache state
            if disc_physically_present:
                action = "insert"
                logger.info(f"Physical detection: disc IS present at {device} -> treating change as INSERT")
            else:
                action = "eject"
                logger.info(f"Physical detection: disc NOT present at {device} -> treating change as EJECT")
                # "Nothing readable" covers two very different situations: an
                # empty tray, and a disc the drive senses but cannot engage —
                # upside down, misseated, damaged. Both used to be handled as
                # a silent eject, so inserting a disc upside down produced no
                # message at all. Only the second case alerts.
                try:
                    from core.media_diagnostics import (
                        medium_present_but_unreadable,
                        notify_unreadable_medium,
                    )

                    if medium_present_but_unreadable(device):
                        logger.warning(
                            "Drive at %s senses media it cannot read — "
                            "likely misseated or damaged; alerting the user",
                            device,
                        )
                        notify_unreadable_medium(device)
                except Exception as exc:
                    logger.debug("unreadable-medium check failed for %s: %s", device, exc)

            # Old cache-based logic removed - was causing inverted detection
            cached_disc = None
            cache_key_found = None
        
        if action == "eject":
            logger.info(f"Processing EJECT event: device={device}, disc_num={disc_num}")

            # Capture disc_id and canonical disc_num from cache BEFORE clearing.
            # handle_disc_eject_for_device clears the cache, so after the call
            # these values are gone.  We need them for the WebSocket message.
            _pre_eject_disc_id = None
            _pre_eject_canonical_disc_num = None
            try:
                from core.disc_cache import get_by_mount_point as _get_by_mp
                _pre_cached = _get_by_mp(device) if device else None
                if _pre_cached:
                    _pre_eject_disc_id = _pre_cached.get("disc_id")
                    _pre_eject_canonical_disc_num = _pre_cached.get("disc_num")
            except Exception:
                pass

            # Resolve cache by device path (like events.drive_eject); udev srN != MakeMKV index.
            if device and device.startswith("/dev/"):
                result = handle_disc_eject_for_device(device, disc_num)
            elif disc_num:
                result = handle_disc_eject(disc_num)
            else:
                result = {"status": "ok", "message": "Eject processed"}
            # Mark all running jobs for this disc as failed
            # Only use disc_hash from cache (returned by handle_disc_eject)
            disc_hash = result.get("disc_hash") if isinstance(result, dict) else None
            failed_job_ids: list = []
            try:
                from api import database
                from api.routers.jobs import _fail_jobs_for_disc
                db = database.SessionLocal()
                try:
                    if disc_hash:
                        failed_job_ids = _fail_jobs_for_disc(
                            disc_hash=disc_hash, db=db, reason="disc ejected"
                        )
                    elif device and device.startswith("/dev/"):
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
            if not disc_hash and not failed_job_ids and device:
                logger.warning(
                    "Eject for device=%s: no disc_hash in cache and no jobs failed by mount_point "
                    "(may be idle drive)",
                    device,
                )
            
            # Track ejection time for cooldown period and clear insertion tracking
            # Use device (mount_point) as key — stable physical identity
            if device:
                _recent_ejections[device] = time.time()
                if device in _recent_insertions:
                    del _recent_insertions[device]
                logger.info(f"Recorded ejection time for device={device}, cooldown={_EJECT_COOLDOWN_SECONDS}s")
            
            # Emit disc_ejected WebSocket event
            # Use disc_id and canonical disc_num captured BEFORE cache was cleared.
            # disc_num from udev (srN suffix) can collide with another drive's MakeMKV
            # index, so we must use the canonical value from cache.
            try:
                import asyncio
                from api.routers.websockets import _emit_to_coordinator

                eject_disc_id = _pre_eject_disc_id
                # Use canonical disc_num from cache (not udev's srN suffix which can collide)
                eject_disc_num = _pre_eject_canonical_disc_num or (
                    result.get("canonical_disc_num") if isinstance(result, dict) else None
                ) or disc_num

                # Get the app's event loop
                app_ref = _app_instance
                if app_ref and hasattr(app_ref, 'state') and hasattr(app_ref.state, 'event_loop'):
                    loop = app_ref.state.event_loop

                    # Create coroutine and schedule it
                    async def emit_eject():
                        await _emit_to_coordinator("disc_ejected", {
                            "disc_id": eject_disc_id,
                            "disc_num": str(eject_disc_num) if eject_disc_num else None,
                            "mount_point": device,
                            "disc_hash": disc_hash,
                            "failed_job_ids": failed_job_ids,
                        })
                        logger.info(
                            f"Emitted disc_ejected via WebSocket: disc_num={eject_disc_num}, "
                            f"disc_id={eject_disc_id}, mount_point={device}, disc_hash={disc_hash}"
                        )

                    asyncio.run_coroutine_threadsafe(emit_eject(), loop)
                else:
                    logger.warning("No event loop available to emit disc_ejected WebSocket event")
            except Exception as emit_exc:
                logger.warning(f"Failed to emit disc_ejected WebSocket event: {emit_exc}")
            
            # Trigger async rescan to notify SSE clients (legacy)
            try:
                import asyncio
                from api.routers.events import _trigger_drive_rescan_async
                # Get the app's event loop if available
                app_ref = _app_instance
                if app_ref and hasattr(app_ref, 'state') and hasattr(app_ref.state, 'event_loop'):
                    loop = app_ref.state.event_loop
                    asyncio.run_coroutine_threadsafe(_trigger_drive_rescan_async(device, "2", "udev_eject"), loop)
                else:
                    # Fallback: try to get running loop
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(_trigger_drive_rescan_async(device, "2", "udev_eject"))
                    except RuntimeError:
                        # No running loop - log warning and skip
                        logger.warning("No event loop available to schedule rescan task")
            except Exception as rescan_exc:
                logger.warning(f"Failed to trigger rescan after eject: {rescan_exc}")
            return result
        elif action == "insert":
            logger.info(f"Processing INSERT event: device={device}, disc_num={disc_num}")
            # Extract mount_point from device path (e.g., "/dev/sr1")
            mount_point = device if device.startswith("/dev/") else device
            # Use provided disc_num or default to "9999" (will be identified during scan)
            disc_num_to_use = disc_num if disc_num else "9999"
            
            # Check for spurious reinsertion after recent eject (USB drives may physically reingest)
            # Use device (mount_point) as key — stable physical identity
            if device and device in _recent_ejections:
                time_since_eject = time.time() - _recent_ejections[device]
                if time_since_eject < _EJECT_COOLDOWN_SECONDS:
                    logger.warning(
                        f"Ignoring spurious insert for device={device} "
                        f"({time_since_eject:.2f}s after eject, cooldown={_EJECT_COOLDOWN_SECONDS}s). "
                        f"This prevents USB drives from auto-reingesting discs."
                    )
                    return {"status": "ok", "message": f"Insert ignored (cooldown period)", "disc_num": disc_num, "mount_point": device}
                else:
                    # Cooldown expired, remove from tracking
                    del _recent_ejections[device]
                    logger.info(f"Cooldown expired for device={device}, processing insert normally")

            # udev "change" with media still present after a stable scan is often SCSI settle noise,
            # not a physical reinsert. Skip full scan when hash matches cache; never hash during rip.
            # Use mount_point for slot state and lock checks (stable physical identity).
            if raw_udev_action == "change" and mount_point:
                from core.disc_slot_state import should_treat_change_as_weak_insert
                from core.disc_locks import (
                    is_operation_active,
                    OPERATION_RIP,
                    OPERATION_HASH,
                    OPERATION_INFO,
                )

                if should_treat_change_as_weak_insert(mount_point):
                    if (
                        is_operation_active(mount_point, OPERATION_RIP)
                        or is_operation_active(mount_point, OPERATION_HASH)
                        or is_operation_active(mount_point, OPERATION_INFO)
                    ):
                        logger.info(
                            "Weak udev change skipped: drive busy (rip/hash/info) mount_point=%s",
                            mount_point,
                        )
                        return {
                            "status": "ok",
                            "message": "Weak media change skipped (drive busy)",
                            "skipped_weak_udev_busy": True,
                            "disc_num": disc_num,
                            "mount_point": mount_point,
                        }

                    cached = cache_get(mount_point)
                    baseline = None
                    if cached:
                        baseline = cached.get("disc_hash") or cached.get("content_hash")
                    if baseline:
                        try:
                            from core.utils import hash_media_disc

                            new_hash = hash_media_disc(mount_point, allow_reentrant=False)
                        except Exception as hash_exc:
                            logger.warning(
                                "Weak udev change: hash probe failed (%s); running full insert",
                                hash_exc,
                            )
                        else:
                            # #720: only skip when the cached disc was ACTUALLY
                            # scanned. A disc whose hash landed in the cache but
                            # whose scan never completed would otherwise be
                            # skipped forever — every udev event says "known
                            # disc, don't rescan" and it stays titleless, so the
                            # UI offers it but Start Copy fails with "no tracks
                            # enumerated". Cached-but-unscanned => fall through
                            # and run the full insert (which scans).
                            cached_scanned = bool(
                                cached.get("disc_info")
                                or cached.get("titles")
                                or (cached.get("scan_state") == "completed")
                            )
                            if new_hash == baseline and not cached_scanned:
                                logger.info(
                                    "Weak udev change: hash matches cache for mount_point=%s "
                                    "but the cached disc was never scanned — running full insert",
                                    mount_point,
                                )
                            elif new_hash == baseline:
                                logger.info(
                                    "Weak udev change: hash matches cache for mount_point=%s, skipping rescan",
                                    mount_point,
                                )
                                return {
                                    "status": "ok",
                                    "message": "ignored duplicate media change",
                                    "skipped_rescan": True,
                                    "disc_num": disc_num,
                                    "mount_point": mount_point,
                                }

            # Track insertion time for spurious eject protection
            # Use device (mount_point) as key — stable physical identity
            if device:
                _recent_insertions[device] = time.time()
                logger.info(f"Recorded insertion time for device={device}, stabilization={_INSERT_STABILIZATION_SECONDS}s")

            # Call handle_disc_insert with mount_point (now handles everything proactively)
            result = handle_disc_insert(disc_num_to_use, mount_point)
            # Note: No need to trigger reactive rescan - handle_disc_insert now handles:
            # 1. Immediate notification to Disc Manager (for "Loading Disc Info...")
            # 2. Targeted scan sequence (hash → info dev:{mount} + DRV parse + drive-cache upsert), not disc:9999 per insert
            # 3. Completion notification to Disc Manager (for final discinfo)
            # Disc Manager then notifies Backend API, which broadcasts via SSE
            # Only trigger a minimal drive list update to notify SSE clients of drive changes
            if not result.get("skipped_scan_in_progress"):
                try:
                    import asyncio
                    from api.routers.events import _trigger_drive_rescan_async
                    # Get the app's event loop if available
                    app_ref = _app_instance
                    if app_ref and hasattr(app_ref, 'state') and hasattr(app_ref.state, 'event_loop'):
                        loop = app_ref.state.event_loop
                        # Trigger minimal drive list update (disc info loading is handled proactively by handle_disc_insert)
                        asyncio.run_coroutine_threadsafe(_trigger_drive_rescan_async(device, "1", "udev_insert"), loop)
                    else:
                        # Fallback: try to get the event loop from the main thread
                        try:
                            # Get the main thread's event loop if available
                            import threading
                            main_thread = threading.main_thread()
                            if main_thread.is_alive():
                                # Try to access the event loop from the main thread
                                # This is a workaround - we'll use a queue or other IPC mechanism
                                # For now, log and skip
                                logger.warning("Event loop not available in app.state, cannot schedule rescan from background thread")
                        except Exception as fallback_exc:
                            logger.warning(f"Fallback event loop access failed: {fallback_exc}")
                except Exception as rescan_exc:
                    logger.warning(f"Failed to trigger rescan after insert: {rescan_exc}")
            return result
        else:
            return {"status": "error", "message": f"Unknown action: {action}"}
    except Exception as exc:
        logger.error(f"Error handling udev event: {exc}")
        return {"status": "error", "message": str(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _uds_server, _app_instance
    _app_instance = app
    # Store the event loop for use in background threads
    try:
        loop = asyncio.get_running_loop()
        app.state.event_loop = loop
        # Also store in progress_emitter global for Celery worker access
        try:
            from core.progress_emitter import _global_event_loop
            import core.progress_emitter as progress_emitter_module
            progress_emitter_module._global_event_loop = loop
        except Exception as exc:
            logging.warning(f"Failed to store event loop in progress_emitter global: {exc}")
        # Also store in app.state for other uses
        app.state.event_loop = loop
        logger = get_logger(__name__, "lifespan")
        logger.info(f"Stored event loop in app.state.event_loop (running={loop.is_running()})")
    except Exception as exc:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to store event loop: {exc}")
    
    # Environment-provided settings are applied on EVERY boot, before anything
    # reads them, so the container is declarative: the environment is the
    # desired state and a restart converges to it. Deliberately not a
    # first-boot-only seed — that would silently stop honouring compose edits
    # once settings.json existed.
    try:
        from core.env_settings import apply_env_settings

        applied = apply_env_settings()
        # The MakeMKV key is not just a stored value: makemkvcon reads it from
        # its own settings.conf, so an env-provided key has to be written there
        # too or an unattended container starts with MakeMKV unregistered.
        if "makemkv_registration_key" in applied:
            try:
                from core.makemkv_updater import _write_app_key_preserving

                _write_app_key_preserving(applied["makemkv_registration_key"])
                get_logger(__name__, "lifespan").info(
                    "Applied MakeMKV registration key from the environment"
                )
            except Exception as exc:
                # Never fatal: the UI can still register the key by hand.
                get_logger(__name__, "lifespan").error(
                    "Could not write the env-provided MakeMKV key to settings.conf: %s", exc
                )
    except Exception as exc:
        get_logger(__name__, "lifespan").warning(
            "Failed to apply environment settings: %s", exc
        )

    # Wire up callbacks for proactive disc insertion flow
    try:
        from core import disc_manager
        from api.routers import events
        
        # Set app reference for event loop access in callbacks
        events.set_app_reference(app)
        
        # Create wrapper function that routes to the appropriate notification function
        def _disc_manager_notification_wrapper(*args, **kwargs):
            """
            Wrapper to route Disc Manager notifications to the appropriate Backend API function.
            - 2 args (disc_num, mount_point) -> disc insertion notification
            - 1 arg (dict) -> disc scan completion notification
            """
            if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], str):
                # Disc insertion: (disc_num, mount_point)
                events._notify_disc_inserted(args[0], args[1])
            elif len(args) == 1 and isinstance(args[0], dict):
                # Disc scan completion: (disc_info_dict)
                events._notify_disc_scan_complete(args[0])
            else:
                logging.warning(f"Unexpected callback arguments: args={args}, kwargs={kwargs}")
        
        # Register wrapper as the callback for both notification types
        disc_manager.register_backend_callback(_disc_manager_notification_wrapper)

        # Register DB-backed DiscDB lookup so query_discdb() checks the database
        # before hitting TheDiscDB API.  This prevents redundant API calls for
        # discs we've already seen and stored in the database.
        from api.crud import get_discdb_data_from_db
        from api.database import SessionLocal

        def _db_discdb_lookup(content_hash: str) -> dict | None:
            db = SessionLocal()
            try:
                return get_discdb_data_from_db(db, content_hash)
            finally:
                db.close()

        disc_manager.register_db_discdb_lookup(_db_discdb_lookup)
        
        logger = get_logger(__name__, "lifespan")
        logger.info("Wired up callbacks for proactive disc insertion flow")
    except Exception as exc:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to wire up callbacks: {exc}")
    
    # Start UDS server for udev events
    try:
        from drive_manager.uds_server import UDSServer
        logger = get_logger(__name__, "lifespan")
        _uds_server = UDSServer(_handle_udev_event)
        _uds_server.start()
        logger.info("UDS server started for udev events")
    except Exception as exc:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to start UDS server: {exc}")
        _uds_server = None

    # Drive warmup must NOT block lifespan startup: Uvicorn only serves HTTP after ``yield`` below.
    # A long makemkvcon scan (multiple trays / slow drives) would otherwise keep the API unreachable.
    async def _deferred_startup_drive_warmup():
        try:
            from core.startup_discs import run_startup_drive_warmup_if_makemkv_ready

            loop_warm = asyncio.get_running_loop()
            warm_logger = get_logger(__name__, "lifespan")
            drives_snapshot = await loop_warm.run_in_executor(
                None, run_startup_drive_warmup_if_makemkv_ready
            )
            warm_logger.info("Startup drive enumeration and rescan complete: %s", drives_snapshot)
            # #613: the warmup populated the disc cache via handle_disc_insert
            # per drive, but the frontend coordinator never knows the cache
            # changed unless a udev event fires. Emit makemkv_drives_ready so
            # the carousel + setup wizard refetch /drives/drives and the disc
            # list. Without this, a disc already-loaded at cold boot stays
            # invisible until the user physically ejects + reinserts it.
            try:
                from api.routers.websockets import _emit_to_coordinator
                await _emit_to_coordinator(
                    "makemkv_drives_ready",
                    {
                        "drives_count": len(drives_snapshot or []),
                        "source": "lifespan",
                    },
                )
            except Exception as emit_exc:
                warm_logger.warning(
                    "Failed to emit makemkv_drives_ready after lifespan warmup: %s",
                    emit_exc,
                )
        except Exception as warm_exc:
            get_logger(__name__, "lifespan").warning(
                "Startup drive enumeration skipped or failed: %s", warm_exc
            )

    # Startup tasks
    _recover_inflight_jobs()
    _startup_cleanup_terminal_jobs()
    _startup_reconcile_dvd_segment_groups()
    
    # Validate MakeMKV installation on startup
    try:
        from core.makemkv_updater import validate_makemkv_installation
        logger = get_logger(__name__, "lifespan")
        makemkv_validation = await asyncio.get_running_loop().run_in_executor(None, validate_makemkv_installation)
        
        if not makemkv_validation["is_valid"]:
            logger.warning(
                "MakeMKV installation is incomplete or broken. "
                "Missing components: %s. Error: %s",
                makemkv_validation["missing_components"],
                makemkv_validation["error_message"]
            )
            # Emit WebSocket notification about invalid installation
            try:
                from api.routers.websockets import emit_notification
                await emit_notification(
                    f"MakeMKV not properly installed: {makemkv_validation['error_message']}", 
                    "error", 
                    "makemkv_invalid"
                )
            except Exception:
                pass  # Don't fail startup if notification fails
        else:
            logger.info(
                "MakeMKV installation validated successfully (version: %s)",
                makemkv_validation["installed_version"] or "unknown"
            )
            # Check key expiration (#35)
            try:
                from core.makemkv_updater import get_registration_status
                expired, msg, _ = await asyncio.get_running_loop().run_in_executor(
                    None, get_registration_status
                )
                if expired:
                    logger.warning("MakeMKV registration key is expired or evaluation period ended")
                    try:
                        from api.routers.websockets import emit_notification
                        await emit_notification(
                            "MakeMKV registration key is expired. Please update your key in Settings → MakeMKV.",
                            "warning",
                            "makemkv_key_expired",
                        )
                    except Exception:
                        pass
            except Exception as key_exc:
                logger.debug("Could not check MakeMKV registration status: %s", key_exc)
    except Exception as exc:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to validate MakeMKV installation on startup: {exc}")

    # #625: initialise MakeMKV pre-download state from any manifest on disk so a
    # container restart with cached tars begins in ``ready`` without waiting for
    # the background download hook to fire.
    try:
        from core import makemkv_predownload_state
        makemkv_predownload_state.initialize_from_disk()
    except Exception as exc:
        get_logger(__name__, "lifespan").debug(
            "MakeMKV pre-download state init from disk skipped: %s", exc
        )

    # Check storage for active transfer config
    try:
        from api.database import get_db
        from core.transfer.service import get_active_config, check_storage
        
        db = next(get_db())
        try:
            logger = get_logger(__name__, "lifespan")
            active_config = get_active_config(db)
            if active_config and active_config.mode in ("smb", "nfs", "rsync"):
                logger.info(f"Checking storage for active transfer config: {active_config.name} ({active_config.mode})")
                storage_info, error = check_storage(db, active_config)
                if error:
                    logger.warning(f"Could not check storage for active transfer config: {error}")
                elif storage_info:
                    free_gb = storage_info.get("free", 0) / (1024 ** 3)
                    total_gb = storage_info.get("total", 0) / (1024 ** 3)
                    logger.info(f"Active transfer destination storage: {free_gb:.2f} GB free / {total_gb:.2f} GB total")
        finally:
            db.close()
    except Exception as e:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to check storage on startup: {e}")
    
    # Drive watcher removed - system now uses udev + UDS for drive detection
    
    # Start Redis subscriber for progress updates from Celery workers
    try:
        import redis.asyncio as aioredis
        # Honor REDIS_URL so E2E runs (and any deploy that overrides the broker
        # location) reach the right Redis instance. Falls back to the production
        # default. DB index defaults to 2 (cache / progress) when the URL omits
        # one — matches core.redis_cache / core.notifications.
        _redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/2")
        if _redis_url.rstrip("/").rsplit("/", 1)[-1].isdigit() is False:
            _redis_url = _redis_url.rstrip("/") + "/2"
        redis_client = aioredis.from_url(_redis_url, decode_responses=True)
        pubsub = redis_client.pubsub()
        
        async def _redis_progress_subscriber():
            """Subscribe to Redis progress update channels and emit via WebSocket."""
            logger = get_logger(__name__, "_redis_progress_subscriber")
            await pubsub.psubscribe("progress_updates:*", "coordinator_events")
            logger.info("Started Redis subscriber for progress updates + coordinator events")
            try:
                while True:
                    message = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                    if message:
                        try:
                            channel = message['channel']
                            data = json.loads(message['data'])
                            if channel == 'coordinator_events':
                                # Worker-side stage transitions (#839): the
                                # celery process has no WS loop, so it
                                # publishes here and we fan out.
                                event_type = data.pop('type', None)
                                if event_type:
                                    from api.routers.websockets import _emit_to_coordinator
                                    await _emit_to_coordinator(event_type, data)
                                continue
                            job_id = data.get('job_id')
                            if job_id:
                                from api.routers.websockets import _emit_job_progress
                                progress_data = {k: v for k, v in data.items() if k != 'job_id'}
                                await _emit_job_progress(job_id, progress_data)
                        except Exception as exc:
                            logger.warning(f"Error processing Redis progress message: {exc}")
            except asyncio.CancelledError:
                pass
            finally:
                await pubsub.unsubscribe()
                await pubsub.close()
                await redis_client.close()
        
        # Start the subscriber task
        subscriber_task = asyncio.create_task(_redis_progress_subscriber())
        app.state.redis_subscriber_task = subscriber_task
        logger = get_logger(__name__, "lifespan")
        logger.info("Started Redis progress subscriber task")
    except Exception as exc:
        logger = get_logger(__name__, "lifespan")
        logger.warning(f"Failed to start Redis progress subscriber: {exc}")
    
    # Start periodic stale job cleanup (every 60 seconds).
    # Previously ran synchronously on every GET /jobs, holding a DB session for 2-3s
    # while doing Celery inspect() + Redis AsyncResult checks.
    async def _periodic_stale_job_cleanup():
        _cleanup_logger = get_logger(__name__, "_periodic_stale_job_cleanup")
        while True:
            await asyncio.sleep(60)
            try:
                loop = asyncio.get_running_loop()
                def _run_cleanup():
                    db = database.SessionLocal()
                    try:
                        from api.routers.jobs import _cleanup_stale_jobs
                        failed_ids = _cleanup_stale_jobs(db)
                        if failed_ids:
                            _cleanup_logger.info(
                                "Periodic cleanup: marked %d job(s) failed during stale/orphan cleanup: %s",
                                len(failed_ids),
                                failed_ids,
                            )
                    finally:
                        db.close()
                await loop.run_in_executor(None, _run_cleanup)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _cleanup_logger.warning("Periodic stale job cleanup failed: %s", exc)
    
    stale_cleanup_task = asyncio.create_task(_periodic_stale_job_cleanup())
    app.state.stale_cleanup_task = stale_cleanup_task
    logger = get_logger(__name__, "lifespan")
    logger.info("Started periodic stale job cleanup task (60s interval)")

    # Eject on restart (#21): if enabled, eject all known disc drives on startup
    try:
        from core.settings import load_settings as _load_settings_restart
        if _load_settings_restart().get("eject_on_restart"):
            from core.disc_cache import get_cached_discs
            from core.utils import eject_disc
            _restart_logger = get_logger(__name__, "lifespan")
            for cached in get_cached_discs():
                mp = cached.get("mount_point")
                if mp:
                    _restart_logger.info("eject_on_restart: ejecting %s", mp)
                    await asyncio.get_running_loop().run_in_executor(None, eject_disc, mp)
    except Exception as eject_exc:
        get_logger(__name__, "lifespan").debug("eject_on_restart check failed: %s", eject_exc)

    # Schedule drive map + optional per-disc rescans after HTTP is accepting (see _deferred_startup_drive_warmup).
    app.state.startup_warmup_task = asyncio.create_task(_deferred_startup_drive_warmup())
    get_logger(__name__, "lifespan").info("Scheduled background startup drive warmup")

    # #625: pre-download MakeMKV source tarballs so the Setup Assistant can link to
    # the real EULA before the user clicks Install. Idempotent (skips fetch when
    # tars already cached), non-blocking, and skipped entirely when MakeMKV is
    # already installed — the EULA link only renders in the not-installed phase.
    async def _deferred_startup_makemkv_predownload():
        try:
            from core import makemkv_predownload_state

            pd_logger = get_logger(__name__, "lifespan")
            await asyncio.get_running_loop().run_in_executor(
                None,
                lambda: makemkv_predownload_state.run_predownload_if_needed(logger=pd_logger),
            )
        except Exception as outer_exc:
            get_logger(__name__, "lifespan").warning(
                "MakeMKV pre-download hook errored: %s", outer_exc
            )

    app.state.startup_makemkv_predownload_task = asyncio.create_task(
        _deferred_startup_makemkv_predownload()
    )
    get_logger(__name__, "lifespan").info("Scheduled background MakeMKV source pre-download")

    yield
    
    # Shutdown tasks
    if hasattr(app.state, "startup_warmup_task"):
        app.state.startup_warmup_task.cancel()
        try:
            await app.state.startup_warmup_task
        except asyncio.CancelledError:
            pass
        get_logger(__name__, "lifespan").info("Cancelled startup drive warmup task")
    if hasattr(app.state, "startup_makemkv_predownload_task"):
        app.state.startup_makemkv_predownload_task.cancel()
        try:
            await app.state.startup_makemkv_predownload_task
        except asyncio.CancelledError:
            pass
        get_logger(__name__, "lifespan").info("Cancelled MakeMKV pre-download task")
    # Stop stale job cleanup
    if hasattr(app.state, 'stale_cleanup_task'):
        app.state.stale_cleanup_task.cancel()
        try:
            await app.state.stale_cleanup_task
        except asyncio.CancelledError:
            pass
        logger = get_logger(__name__, "lifespan")
        logger.info("Stopped periodic stale job cleanup task")
    
    # Stop Redis subscriber
    if hasattr(app.state, 'redis_subscriber_task'):
        app.state.redis_subscriber_task.cancel()
        try:
            await app.state.redis_subscriber_task
        except asyncio.CancelledError:
            pass
        logger = get_logger(__name__, "lifespan")
        logger.info("Stopped Redis progress subscriber task")
    
    # Stop UDS server
    if _uds_server:
        try:
            logger = get_logger(__name__, "lifespan")
            _uds_server.stop()
            logger.info("UDS server stopped")
        except Exception as exc:
            logger = get_logger(__name__, "lifespan")
            logger.warning(f"Error stopping UDS server: {exc}")
        _uds_server = None
    
    # Lock files removed - using in-memory state tracking instead


app = FastAPI(lifespan=lifespan)

# Readiness gate — returns 503 + Retry-After for non-allowlisted routes when the DB is
# still recovering on startup. Without this, the FastAPI app accepts traffic before
# Postgres finishes WAL recovery and every endpoint 500s with
# "FATAL: the database system is not yet accepting connections". The frontend reads
# those as CORS errors (the 500 from the route handler doesn't get CORS headers in
# Starlette's default exception path) and shows a dead page until manual refresh.
#
# A small TTL cache avoids hammering Postgres with SELECT 1 on every request when ready.
_READINESS_ALLOWLIST: tuple[str, ...] = (
    "/readyz",
    "/healthz",
    "/system/health",
    "/system/setup/status",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# Two TTLs so cold-start retains the original short window (faster recovery
# on a flaky Postgres come-up) while warm steady-state caches for 30s and
# skips the SELECT 1 entirely on the vast majority of requests. The cold
# TTL is the value the gate held since #373's introduction; the warm TTL
# is the one this issue (#490) optimizes.
_READINESS_CACHE_TTL_COLD_SECONDS = 2.0
_READINESS_CACHE_TTL_WARM_SECONDS = 30.0
_readiness_state: dict[str, Any] = {"checked_at": 0.0, "ready": False, "error": None}

# #709: the guarded startup migration (Docker/scripts/db-migrate.sh) writes this
# sentinel when `alembic upgrade head` fails, so the DB is left half-migrated.
# While it exists the backend must refuse to serve real traffic — a half-migrated
# schema silently corrupts reads/writes. The readiness check below treats its
# presence as "not ready", which the readiness_gate middleware turns into a 503
# for every non-allowlisted (mutating) route. Cleared by a later successful
# migration run. Env-overridable so tests don't touch a real /data path.
_MIGRATION_FAILED_SENTINEL = os.getenv(
    "MKVAUTO_MIGRATION_SENTINEL", "/data/.mkvauto-migration-failed"
)


def _migration_failure_reason() -> Optional[str]:
    """Return the migration-failure detail if the sentinel exists, else None.

    A bare stat on the happy path (sentinel absent) is a cheap syscall; the file
    is only read when it actually exists (a failed upgrade — rare)."""
    try:
        if not os.path.exists(_MIGRATION_FAILED_SENTINEL):
            return None
        with open(_MIGRATION_FAILED_SENTINEL, "r", encoding="utf-8") as fh:
            detail = fh.read().strip()
        return detail or "database migration failed"
    except OSError:
        return None

# Coalesces concurrent cold-cache callers. Without this, a fresh page load
# fires N concurrent middleware passes that each open their own
# SessionLocal + SELECT 1 round-trip while the event loop is blocked,
# serializing the request burst instead of dispatching to handlers in
# parallel. With the lock, the first caller pays the round-trip and every
# other concurrent caller awaits the same result. LoopLocalLock because a
# module-level asyncio.Lock sticks to the first loop that contends on it;
# see core/loop_local.py.
_readiness_lock = LoopLocalLock()


def _readiness_ttl_for_current_state() -> float:
    """Effective TTL for the readiness cache: warm path is 30s, cold path 2s.

    Keeping the cold TTL short means a startup window where Postgres is
    still in WAL recovery resolves within ~2s of becoming ready — not 30s.
    """
    return (
        _READINESS_CACHE_TTL_WARM_SECONDS
        if _readiness_state["ready"]
        else _READINESS_CACHE_TTL_COLD_SECONDS
    )


def _ping_db_blocking() -> None:
    """Synchronous SELECT 1 against the app DB. Called from an executor by
    the async readiness check so the event loop stays unblocked."""
    sess = database.SessionLocal()
    try:
        sess.execute(text("SELECT 1"))
    finally:
        sess.close()


def _check_db_ready() -> tuple[bool, Optional[str]]:
    """Cached SELECT 1 ping (sync). Returns (ready, error_string_or_None).

    Retained as the sync façade so tests, scripts, and any non-async caller
    can still drive the readiness check without an event loop. The async
    middleware uses ``_check_db_ready_async`` which adds executor offload
    and coalescing — both irrelevant in a sync context.
    """
    mig = _migration_failure_reason()
    if mig is not None:
        _readiness_state["ready"] = False
        _readiness_state["error"] = mig
        return False, f"migration_failed: {mig}"
    now = time.monotonic()
    state = _readiness_state
    if state["ready"] and (now - state["checked_at"]) < _readiness_ttl_for_current_state():
        return True, None
    try:
        _ping_db_blocking()
        state["checked_at"] = now
        state["ready"] = True
        state["error"] = None
        return True, None
    except Exception as exc:
        state["checked_at"] = now
        state["ready"] = False
        state["error"] = str(exc)
        return False, str(exc)


async def _check_db_ready_async() -> tuple[bool, Optional[str]]:
    """Async readiness check. Three differences vs the sync version:

    1. The SELECT 1 round-trip runs in the default executor so the event
       loop stays available for other concurrent requests.
    2. Concurrent cold-cache callers serialize behind an ``asyncio.Lock``
       so a burst of N requests pays at most one DB round-trip per TTL
       window.
    3. Each lock-holder re-checks the cache before pinging, in case
       another caller refreshed it while we were waiting on the lock.
    """
    mig = _migration_failure_reason()
    if mig is not None:
        _readiness_state["ready"] = False
        _readiness_state["error"] = mig
        return False, f"migration_failed: {mig}"
    state = _readiness_state
    now = time.monotonic()
    if state["ready"] and (now - state["checked_at"]) < _readiness_ttl_for_current_state():
        return True, None

    async with _readiness_lock:
        # Recheck: another caller may have just refreshed while we waited.
        recheck_now = time.monotonic()
        if state["ready"] and (recheck_now - state["checked_at"]) < _readiness_ttl_for_current_state():
            return True, None

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, _ping_db_blocking)
            state["checked_at"] = time.monotonic()
            state["ready"] = True
            state["error"] = None
            return True, None
        except Exception as exc:
            state["checked_at"] = time.monotonic()
            state["ready"] = False
            state["error"] = str(exc)
            return False, str(exc)


@app.middleware("http")
async def readiness_gate(request, call_next):
    """Gate non-allowlisted routes behind a DB readiness check.

    Returns 503 + Retry-After: 5 with explicit CORS headers when the DB is unavailable
    so the frontend sees a clean readiness signal instead of a CORS-stripped 500.
    """
    path = request.url.path
    method = request.method
    if method == "OPTIONS" or path in _READINESS_ALLOWLIST or path.startswith("/static"):
        return await call_next(request)
    ready, err = await _check_db_ready_async()
    if not ready:
        from fastapi.responses import JSONResponse

        # CORS allow_origins is "*" so we mirror that here. Without these headers the
        # browser drops the response and shows a generic CORS failure.
        headers = {
            "Retry-After": "5",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Expose-Headers": "Retry-After",
            "Cache-Control": "no-store",
        }
        return JSONResponse(
            status_code=503,
            content={"status": "starting", "detail": "backend warming up", "error": err},
            headers=headers,
        )
    return await call_next(request)


# CORS — allow all origins (dev / trusted network). When the frontend gets status 0 or
# "access control checks", the request often never reached the backend (not running or
# not listening on 0.0.0.0). See docs/Guides/TROUBLESHOOTING_FRONTEND.md.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,  # must be False when allow_origins is "*"
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Include your API routers
app.include_router(jobs.router)
app.include_router(events.router)
app.include_router(system.router)
app.include_router(discs.router)
app.include_router(discdb.router)
app.include_router(releases.router)
app.include_router(movies.router)
app.include_router(drives.router)  # Internal drive operations
from api.routers import disc_previews
app.include_router(disc_previews.router)  # Disc-scoped durable preview serving (#355)
app.include_router(websockets.router)  # WebSocket endpoints for workflow contexts
app.include_router(websockets.http_router)  # HTTP endpoints for coordinator (fallback)

@app.get("/healthz")
async def healthz():
    return {"status": "ok"}

@app.get("/readyz")
async def readyz():
    """Readiness probe: 200 when the backend can serve real traffic, 503 otherwise.

    The frontend polls this on bootstrap to decide whether to show a "Setting Up"
    overlay; the readiness_gate middleware uses the same underlying check to fence
    other routes during Postgres WAL recovery.
    """
    from fastapi.responses import JSONResponse

    ready, err = await _check_db_ready_async()
    if not ready:
        return JSONResponse(
            status_code=503,
            content={"status": "starting", "error": err},
            headers={"Retry-After": "5", "Cache-Control": "no-store"},
        )
    return {"status": "ok"}


# We'll stash the background task here so we can cancel it on shutdown

def _recover_inflight_jobs() -> None:
    """
    On startup, re-dispatch any jobs that were marked running/pending so
    they can resume after a crash/restart.
    
    NOTE: Rip recovery has been removed to prevent infinite requeue loops.
    Only post-processing recovery and output validation are handled here.
    
    First, fail all jobs with active rip_state (they can't continue after restart).
    """
    logger = get_logger(__name__, "_recover_inflight_jobs")
    from core.job_paths import JobPaths

    db = database.SessionLocal()
    try:
        # Reconcile in-flight rips: fail truly orphaned jobs, or re-enqueue verification when
        # copy already finished in DB but the worker never called rip-complete.
        from api.routers.jobs import _fail_orphaned_rip_jobs_on_startup
        failed_ids = _fail_orphaned_rip_jobs_on_startup(db)
        if failed_ids:
            logger.info("Failed %d orphaned rip job(s) on startup (service restart detected): %s", len(failed_ids), failed_ids)
        
        # Include "validating" to recover jobs stuck mid-validation from a previous crash.
        jobs = (
            db.query(db_models.Job)
              .filter(db_models.Job.job_status.in_(["pending", "running", "validating"]))
              .all()
        )
        for job in jobs:
            job_id = str(job.id)
            rip_state = getattr(job, "rip_state", None)
            # #365 — derived, not column. (Currently this local is unused
            # downstream; kept for symmetry with rip_state/job_status and
            # in case a future edit branches on it.)
            post_state = job.derived_post_state
            job_status = getattr(job, "job_status", None)
            
            # No automatic requeue of postprocess on startup (user retries after callback/worker failure).

            # Reconcile artifacts only for jobs stuck in "validating" status from a
            # previous interrupted startup validation. Once rip_state is "completed",
            # the rip-complete callback already validated the output — re-validating
            # here for "running" jobs is unnecessary and creates a race condition with
            # post-processing (which moves files from raw/ to transient/, causing
            # gather_final_outputs to see "missing" files and fail the job).
            #
            # Defense against #366: if transfer has already completed, the local
            # transient/ is legitimately cleaned up and there is nothing to reconcile.
            # Sanitize the stale "validating" sub-state back to "running" so the job
            # is not failed by the local-reconciliation branch below and is not stuck
            # being re-evaluated by recovery on every restart. We do not force
            # "completed" here because miss-profile invariants may require label/
            # finalize/finalize_release stages that we cannot verify from this context.
            transfer_state = getattr(job, "transfer_state", None)
            if (job_status == "validating"
                    and transfer_state == "completed"):
                try:
                    apply_job_state(
                        db,
                        job,
                        updates={"job_status": "running"},
                        reason="startup recovery: transfer already completed, clearing stale validating",
                    )
                    logger.info(
                        "Job %s: cleared stale job_status=validating; transfer_state already completed",
                        job_id,
                    )
                except Exception:
                    db.rollback()
                continue
            if (job_status == "validating"
                    and job.rip_progress >= 100
                    and rip_state in ("completed", "skipped")):
                job_paths = JobPaths.for_id(str(job.id))
                if job_paths.root.exists():
                    post_paths = getattr(job, "post_paths", None)
                    ripped_files = getattr(job, "ripped_files", None)
                    if post_paths:
                        # #365 — under MKVAUTO_RENAME_DIRECT_TO_DEST=1 the
                        # rename wrote post_paths' files to config.transfer_dir,
                        # not to transient/. The resolver returns the path
                        # rename actually used (transient/ under flag-off and
                        # for remote modes; config.transfer_dir under flag-on
                        # local). Without this, restart recovery walks the
                        # empty transient/ and marks the job failed even
                        # though the files exist at the library destination.
                        from core.transfer.path_resolution import resolve_transfer_prep_validation_root
                        root = resolve_transfer_prep_validation_root(job, job_paths, db)
                        paths = post_paths
                        output_field = "post_paths"
                    else:
                        root = job_paths.raw
                        paths = ripped_files
                        output_field = "ripped_files"

                    if root.exists() or paths:

                        def _background_validate(
                            job_id_str: str,
                            root_path: Path,
                            paths: dict | None,
                            field_name: str,
                        ) -> None:
                            session = database.SessionLocal()
                            try:
                                job_row = crud.get_job(session, job_id_str)
                                if not job_row:
                                    return
                                # Safety re-check: bail out if job status changed since we
                                # decided to validate (e.g. user triggered postprocess).
                                current_status = getattr(job_row, "job_status", None)
                                if current_status not in ("running", "validating"):
                                    logger.info(
                                        "Skipping startup validation for job %s: status already changed to %s",
                                        job_id_str, current_status,
                                    )
                                    return
                                try:
                                    apply_job_state(session, job_row, updates={"job_status": "validating"}, reason="startup validation")
                                    # gather_final_outputs now returns title_id keys
                                    final_paths_local, _hashes = gather_final_outputs(root_path, paths, disc_id=getattr(job_row, "disc_id", None), db=session)
                                    apply_job_state(
                                        session,
                                        job_row,
                                        updates={
                                            # Do not force job completion here; just reconcile artifacts.
                                            # The job may still require labeling/transfer/finalize_release.
                                            "job_status": "running",
                                            "rip_progress": 100,
                                            field_name: final_paths_local or None,
                                            "error_reason": None,
                                        },
                                        reason="startup reconciliation: outputs validated via hash",
                                    )
                                    logger.info("Recovered stuck job %s by validating outputs", job_id_str)
                                except Exception as exc:
                                    try:
                                        apply_job_state(
                                            session,
                                            job_row,
                                            updates={"job_status": "failed", "error_reason": f"Startup validation failed: {exc}"},
                                            reason="startup validation failed",
                                        )
                                    except Exception:
                                        session.rollback()
                                    logger.warning("Failed to validate outputs for job %s: %s", job_id_str, exc)
                            finally:
                                session.close()

                        threading.Thread(
                            target=_background_validate,
                            args=(job_id, root, paths, output_field),
                            daemon=True,
                        ).start()
                        continue

            # Skip rows that have nowhere to resume from (e.g., job_dir purged).
            computed_job_root = JobPaths.for_id(str(job.id)).root
            if not computed_job_root.exists():
                logger.info("Skipping recovery for job %s: job dir missing (%s)", job.id, computed_job_root)
                try:
                    apply_job_state(
                        db,
                        job,
                        updates={"job_status": "failed", "error_reason": "Recovery skipped: working directory missing"},
                        reason="startup recovery skipped",
                    )
                except Exception:
                    db.rollback()
                continue
            
            # RIP RECOVERY REMOVED: Jobs in "pending" or "running" state with incomplete rips
            # will remain in that state and can be manually retried by the user if needed.
            # This prevents infinite requeue loops that were causing duplicate rips.
            logger.debug("Skipping rip recovery for job %s (status=%s, rip_state=%s) - rip recovery has been disabled", job.id, job.job_status, rip_state)
    except Exception as exc:
        logger.warning("Job recovery failed: %s", exc)
    finally:
        db.close()

def _startup_reconcile_dvd_segment_groups() -> None:
    """One pass over DVD discs so rows demoted by segment-map grouping heal
    without waiting for a label save (#831).

    ``duplicate_group_sync`` and Path B used to treat a DVD's segment map as
    content identity and hid every same-shape episode under one "primary".
    Both now stand down on DVD, and each *writes* through the same two
    entry points — but those run on scan ingest and label patches, and the
    workflow-context GET is deliberately read-only. A disc already on the
    shelf would therefore stay collapsed until the user next touched it.
    Running the two passes here (idempotent, ~30 discs, milliseconds) means
    the first context load after an upgrade already shows every title.
    """
    logger = get_logger(__name__, "_startup_reconcile_dvd_segment_groups")
    try:
        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc
        from core.path_b_dedupe import apply_path_b_marks_for_disc
        from core.segment_identity import segment_maps_identify_content
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("DVD segment-group reconcile unavailable: %s", exc)
        return
    db = database.SessionLocal()
    healed_discs = 0
    try:
        try:
            discs = db.query(db_models.Disc).filter(db_models.Disc.format.isnot(None)).all()
        except Exception as exc:
            # A startup task must never take the app down with it — e.g. the
            # schema isn't there yet because the DB was recreated under us.
            db.rollback()
            logger.warning("DVD segment-group reconcile skipped: %s", exc)
            return
        for disc in discs:
            if segment_maps_identify_content(disc.format):
                continue
            try:
                released = sync_duplicate_group_labels_for_disc(db, str(disc.id))
                cleared, _, _, _ = apply_path_b_marks_for_disc(db, str(disc.id))
                if released or cleared:
                    healed_discs += 1
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("DVD segment-group reconcile failed for disc %s", disc.id)
        if healed_discs:
            logger.info("DVD segment-group reconcile: healed %s disc(s)", healed_discs)
        else:
            logger.debug("DVD segment-group reconcile: nothing to heal")
    finally:
        db.close()


def _startup_cleanup_terminal_jobs() -> None:
    """
    On startup, enqueue cleanup for terminal jobs that haven't been cleaned yet.

    Finds completed/failed jobs with transfer_source_cleaned=False,
    then enqueues cleanup_job_mkv for each. This ensures jobs that missed cleanup
    (e.g., service crashed after finishing but before cleanup ran) get cleaned
    promptly on the next startup instead of waiting for the daily reconciliation.
    """
    logger = get_logger(__name__, "_startup_cleanup_terminal_jobs")
    db = database.SessionLocal()
    try:
        from core.job_cleanup import job_source_is_safe_to_clean
        uncleaned = (
            db.query(db_models.Job)
            .filter(
                db_models.Job.job_status.in_(["completed", "failed"]),
                db_models.Job.transfer_source_cleaned == False,  # noqa: E712
            )
            .all()
        )
        # A FAILED job whose transfer never completed still holds the ONLY
        # copy of its rip in raw/ — cleaning it at boot destroyed a 48GB UHD
        # rip on prod (failed post-process, transfer pending). cleanup_job_mkv
        # re-checks this, but don't even enqueue.
        uncleaned = [j for j in uncleaned if job_source_is_safe_to_clean(j)]
        if not uncleaned:
            logger.info("No terminal jobs need cleanup on startup")
            return
        from workers.tasks import cleanup_job_mkv
        for job in uncleaned:
            cleanup_job_mkv.delay(str(job.id), "startup_cleanup")
        logger.info("Enqueued cleanup for %d terminal job(s) on startup", len(uncleaned))
    except Exception as exc:
        logger.warning("Startup cleanup of terminal jobs failed: %s", exc)
    finally:
        db.close()


# Startup/shutdown handled by lifespan context manager above
