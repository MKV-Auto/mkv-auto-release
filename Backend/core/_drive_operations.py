"""
INTERNAL MODULE - DO NOT IMPORT DIRECTLY

This module contains low-level drive operations that should ONLY be called by:
- core.disc_manager (for disc information retrieval)
- api.routers.drives (for internal HTTP endpoints)

DO NOT:
- Import this module in API routers (use disc_manager instead)
- Call these functions from workers (use disc_manager instead)
- Use these functions to solve problems (use disc_manager instead)

Public API: Use core.disc_manager functions instead.
"""
import logging
import os
import sys
import time
import inspect
from typing import List, Tuple, Optional, Dict, Any
from time import perf_counter
from pathlib import Path

from fastapi import HTTPException
from core.disc_cache import (
    get as cache_get,
    set_payload as cache_set,
    clear_key,
    get_by_mount_point,
    clear_keys_by_mount_point,
)
from core.utils import (
    get_drives,
    MakeMKVError,
    hash_media_disc,
    run_makemkv,
    get_disc_size_bytes_for_mount_point,
    build_drive_api_dict,
    parse_drv_fields_for_mount,
    upsert_makemkv_drive_cache_for_mount,
    ensure_makemkv_index_for_mount,
)
from core.logging_utils import get_logger
from core.disc_locks import (
    acquire_operation_lock,
    release_operation_lock,
    is_operation_active,
    OPERATION_HASH,
    OPERATION_INFO,
    OPERATION_RIP,
)
from core.disc_slot_state import (
    mark_slot_absent,
    mark_slot_stable,
    try_begin_insert_scan,
    end_insert_scan,
)

log = get_logger("core._drive_operations")

INFO_LOG_DEBUG = os.getenv("MKVAUTO_DEBUG_INFO_LOG", "").lower() in ("1", "true", "yes")
INFO_LOG_DIR = os.getenv(
    "MKVAUTO_INFO_LOG_DIR",
    os.path.join(os.getenv("MKVAUTO_ROOT", "/tmp/MakeMKV-Auto"), "logs", "info_logs"),
)

# Runtime check on import - prevent unauthorized imports
# Only check if we can reliably determine the caller and it's not a system import
_caller = None
try:
    _caller = sys._getframe(1).f_globals.get('__name__', '')
except (AttributeError, ValueError):
    # If we can't get the caller, allow import (e.g., during testing or indirect imports)
    pass

# Allow system imports (importlib, bootstrap, etc.) and known good callers
# Block only direct imports from application modules that shouldn't use this
if _caller and not _caller.startswith((
    'core.disc_manager',
    'api.routers.drives',
    'api.main',  # For udev event handling
    'core._drive_operations',
    'tests.',
    'pytest',
    '__main__',
    'importlib',
    '_bootstrap',
    'builtins',
    'frozen',
    ''  # Empty string for some edge cases
)):
    # Only block if it's clearly an application module trying to import directly
    if '.' in _caller and not _caller.startswith(('importlib', '_bootstrap', 'frozen')):
        raise ImportError(
            f"core._drive_operations cannot be imported from {_caller}. "
            f"Use core.disc_manager instead."
        )


def _internal_only(allowed_callers=None):
    """
    Decorator to restrict function access to specific callers.
    
    Args:
        allowed_callers: List of module names allowed to call this function.
                        If None, only allows calls from core.disc_manager and api.routers.drives
    """
    if allowed_callers is None:
        allowed_callers = [
            'core.disc_manager',
            'core.startup_discs',
            'api.routers.drives',
            'api.main',
            'drive_manager.uds_server',
            'tests',
            '_pytest',
        ]
    
    def decorator(func):
        def wrapper(*args, **kwargs):
            # Get full call stack to find any allowed caller
            stack = inspect.stack()
            caller_module = None
            allowed_caller_found = False
            
            # Walk through the call stack (skip wrapper and decorator frames)
            # Also skip thread executor frames (concurrent.futures.thread) and look deeper
            thread_executor_modules = ['concurrent.futures.thread', 'concurrent.futures', 'threading']
            # Track if we've seen any allowed module in the stack (even if wrapped in executor)
            seen_allowed_modules = []
            for frame_info in stack[1:]:
                mod_name = frame_info.frame.f_globals.get('__name__', '')
                if mod_name and not mod_name.startswith('core._drive_operations'):
                    # Check if this module is in the allowed list (even if it's a thread executor)
                    if any(mod_name.startswith(allowed) for allowed in allowed_callers):
                        seen_allowed_modules.append(mod_name)
                        # If it's not a thread executor, we found our caller
                        if not any(mod_name.startswith(thread_mod) for thread_mod in thread_executor_modules):
                            allowed_caller_found = True
                            caller_module = mod_name
                            break
                    # Skip thread executor frames only if we haven't seen an allowed module yet
                    elif any(mod_name.startswith(thread_mod) for thread_mod in thread_executor_modules):
                        # If we've seen an allowed module earlier, this executor is wrapping it - allow it
                        if seen_allowed_modules:
                            allowed_caller_found = True
                            caller_module = seen_allowed_modules[0]
                            break
                        continue
                    # Also record the first non-internal module we find (for error reporting)
                    if not caller_module:
                        caller_module = mod_name
            
            # Check if an allowed caller was found in the stack
            is_allowed = allowed_caller_found
            
            if not is_allowed:
                log.error(
                    f"SECURITY: {func.__name__} called from unauthorized module: {caller_module}\n"
                    f"Call stack: {inspect.stack()}"
                )
                raise RuntimeError(
                    f"{func.__name__} is internal-only. "
                    f"Use core.disc_manager functions instead. "
                    f"Called from: {caller_module}"
                )
            
            return func(*args, **kwargs)
        wrapper.__name__ = func.__name__
        wrapper.__doc__ = func.__doc__
        return wrapper
    return decorator


def _maybe_dump_info_log(info_log: str | list[str] | None, disc_num: str, disc_hash: str | None) -> None:
    """
    When debug is enabled, persist the raw info log to disk for troubleshooting.
    """
    if not INFO_LOG_DEBUG or not info_log:
        return
    log_text = "\n".join(info_log) if isinstance(info_log, list) else str(info_log)
    if not log_text.strip():
        return
    try:
        os.makedirs(INFO_LOG_DIR, exist_ok=True)
        fname = f"disc_{disc_hash or disc_num}_{int(time.time())}.log"
        path = os.path.join(INFO_LOG_DIR, fname)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(log_text)
        log.info("Saved raw info_log to %s", path)
    except Exception as exc:
        log.debug("Failed to save info_log: %s", exc)


def _tuple_drives(drives: List[Tuple[str, str]]) -> list:
    """Convert drives tuple list to dict list (includes hardware / friendly labels for UI)."""
    return [build_drive_api_dict(num, mp) for num, mp in drives]


def _load_discinfo(disc_num: str, mount_point: str, refresh: bool = False, source: str = "unspecified") -> dict:
    """
    Internal helper to fetch disc info, optionally bypassing cache.
    """
    import json, time
    if not refresh:
        # Look up by mount_point first (primary key), then disc_num (alias)
        cached = cache_get(mount_point) if mount_point else None
        if not cached:
            cached = cache_get(str(disc_num))
        if cached:
            log.info("discinfo cache hit disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
            return cached
        # No cached payload and refresh not requested — do not run makemkv here.
        log.info("discinfo cache miss disc_num=%s mount_point=%s source=%s (no refresh; skipping scan)", disc_num, mount_point, source)
        raise HTTPException(status_code=404, detail="Disc info not cached; trigger rescan to refresh")

    # #562 PR 4: the pre-flight `disc:9999` was vestigial — kept to "refresh
    # path→index map", but the per-disc ``info dev:`` scan below parses DRV
    # from its own output (line ~256) and upserts via
    # ``upsert_makemkv_drive_cache_for_mount``. Calling it here added the
    # global-lock contention that emitted MSG:5010 on a sibling drive's
    # in-flight rip.

    log.info("discinfo scan start disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    
    # State checking is handled by drive manager - if drive is busy, it will return 409
    started = perf_counter()
    
    # Drive operations only return raw info (hash + makemkv info log)
    # No DiscDB queries, no parsing - that's handled by Disc Manager
    logger = get_logger("core._drive_operations", "_load_discinfo")
    try:
        # 1. Calculate hash
        log.info("Calculating hash for disc %s", disc_num)
        logger.debug("About to calculate hash disc_num=%s mount_point=%s", disc_num, mount_point)
        content_hash = hash_media_disc(mount_point, allow_reentrant=False)
        log.info("Hash calculated: %s", content_hash)
        logger.debug("Hash calculated disc_num=%s mount_point=%s content_hash=%s", disc_num, mount_point, content_hash)
        
        # Validate hash before proceeding to makemkv scan
        if not content_hash or not isinstance(content_hash, str) or len(content_hash.strip()) == 0:
            error_msg = f"Hash calculation failed or returned invalid value for disc {disc_num} at {mount_point}"
            log.error(error_msg)
            logger.debug("Hash validation failed - skipping makemkv scan disc_num=%s mount_point=%s content_hash=%s error_msg=%s", 
                        disc_num, mount_point, content_hash, error_msg)
            raise ValueError(error_msg)
        
        # 2. Run makemkv info scan — always info dev:{mount} so the scan targets this device and
        #    DRV lines yield the current MakeMKV index (avoids stale _last_drive_scan disc:N).
        log.info("Running makemkv info scan for device %s", mount_point)
        logger.debug("Starting makemkv info scan disc_num=%s mount_point=%s source=%s refresh=%s", 
                    disc_num, mount_point, source, refresh)
        min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
        info_args = f"info dev:{mount_point} -r --minlength={min_title_len}"
        logger.debug("About to run makemkv disc_num=%s mount_point=%s info_args=%s", disc_num, mount_point, info_args)
        info_log, _ = run_makemkv(info_args)
        logger.debug("makemkv info scan completed disc_num=%s mount_point=%s source=%s info_log_length=%s", 
                    disc_num, mount_point, source, len(str(info_log)) if info_log else 0)
        il_text = info_log if isinstance(info_log, str) else "\n".join(info_log) if isinstance(info_log, list) else str(info_log)
        parsed_idx, parsed_hw, _vol = parse_drv_fields_for_mount(il_text, mount_point)
        if parsed_idx:
            upsert_makemkv_drive_cache_for_mount(mount_point, parsed_idx, parsed_hw)
            canonical = str(parsed_idx)
        else:
            log.warning("Could not parse MakeMKV index from DRV for mount_point=%s; using caller disc_num", mount_point)
            canonical = str(disc_num)
        if parsed_idx and str(disc_num) != str(parsed_idx):
            log.warning(
                "disc_num mismatch vs dev-scan DRV index: disc_num=%s mount=%s parsed=%s (using parsed for cache)",
                disc_num, mount_point, parsed_idx,
            )
        
        # 3. Return raw payload (hash + info log + disc_size_bytes)
        logger.debug("About to return payload disc_num=%s mount_point=%s has_content_hash=%s has_info_log=%s", 
                    canonical, mount_point, bool(content_hash), bool(info_log))
        payload = {
            "disc_num": canonical,
            "mount_point": mount_point,
            "disc_hash": content_hash,
            "content_hash": content_hash,
            "info_log": il_text,
            "raw_info_log": il_text,
        }
        disc_size_bytes = get_disc_size_bytes_for_mount_point(mount_point)
        if disc_size_bytes:
            payload["disc_size_bytes"] = disc_size_bytes

        _maybe_dump_info_log(payload.get("raw_info_log"), canonical, content_hash)
        # Cache by mount_point (primary key for multi-drive correctness)
        cache_set(mount_point, payload)
        
        log.info("discinfo scan completed: canonical=%s hash=%s info_log_length=%s", canonical, content_hash, len(payload.get("info_log", "")))
        return payload
    except Exception as exc:
        log.error("Error during discinfo scan: %s", exc)
        raise
    finally:
        elapsed = perf_counter() - started
        log.info("discinfo scan end disc_num=%s mount_point=%s source=%s elapsed=%.2fs", disc_num, mount_point, source, elapsed)


# Public interface functions (with access control)
@_internal_only()
def list_drives() -> List[Dict[str, str]]:
    """
    Enumerate drives using MakeMKV.
    Internal use only - called by disc_manager or drives router.
    """
    log.info("list_drives() called")
    try:
        drives = get_drives()
        log.info("Drives enumerated: %s", drives)
        return _tuple_drives(drives)
    except MakeMKVError as exc:
        log.warning("Drive scan failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Drive scan failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@_internal_only()
def get_disc_info(disc_num: str, mount_point: str, refresh: bool = False) -> dict:
    """
    Get disc info (cached or scan).
    Internal use only - called by disc_manager or drives router.
    """
    source = "ui_refresh" if refresh else "auto_or_cache"
    log.info("get_disc_info disc_num=%s mount_point=%s refresh=%s source=%s", disc_num, mount_point, refresh, source)
    try:
        return _load_discinfo(disc_num, mount_point, refresh=refresh, source=source)
    except HTTPException as exc:
        logger = get_logger("core._drive_operations", "get_disc_info")
        logger.debug("HTTPException raised disc_num=%s mount_point=%s status_code=%s detail=%s", 
                    disc_num, mount_point, exc.status_code, str(exc.detail))
        raise
    except MakeMKVError as exc:
        logger = get_logger("core._drive_operations", "get_disc_info")
        logger.debug("MakeMKVError raised disc_num=%s mount_point=%s error=%s", disc_num, mount_point, str(exc))
        # Surface an explicit conflict when a scan/hash is already running.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger = get_logger("core._drive_operations", "get_disc_info")
        logger.debug("Exception raised disc_num=%s mount_point=%s error=%s error_type=%s", 
                    disc_num, mount_point, str(exc), type(exc).__name__)
        log.warning("Failed to load disc %s at %s: %s", disc_num, mount_point, exc)
        # propagate DiscDB misses as 404 to match previous API behaviour
        msg = str(exc)
        if "DiscDB" in msg or "no match" in msg:
            raise HTTPException(
                status_code=404,
                detail={"type": "discdb_not_found", "message": "No TheDiscDB entry found for this disc hash"},
            ) from exc
        raise HTTPException(status_code=500, detail=msg) from exc


@_internal_only()
def refresh_disc_info(disc_num: str, mount_point: str) -> dict:
    """
    Force a re-scan of a disc, bypassing any cached payload.
    Internal use only - called by disc_manager or drives router.
    """
    source = "ui_refresh"
    log.info("refresh_disc_info disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    # Note: _load_discinfo will check the scan lock and prevent scans during active rips
    try:
        return _load_discinfo(disc_num=disc_num, mount_point=mount_point, refresh=True, source=source)
    except MakeMKVError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@_internal_only()
def validate_disc_info(disc_num: str, mount_point: str, disc_hash: str) -> dict:
    """
    Return cached disc info for the drive if the cached hash matches the expected hash.
    Does not trigger a scan; instructs the caller to refresh when missing or mismatched.
    Internal use only - called by disc_manager or drives router.
    """
    if not disc_hash:
        raise HTTPException(status_code=400, detail="disc_hash is required")
    # Look up by mount_point first (primary key), then disc_num (alias)
    cached = cache_get(mount_point) if mount_point else None
    if not cached:
        cached = cache_get(str(disc_num))
    if not cached:
        raise HTTPException(status_code=404, detail="Disc info not cached; refresh discinfo first")
    cached_hash = cached.get("disc_hash")
    if not cached_hash:
        raise HTTPException(status_code=409, detail="Cached disc info missing hash; refresh discinfo first")
    if str(cached_hash) != str(disc_hash):
        raise HTTPException(
            status_code=409,
            detail=f"Disc hash mismatch (expected {disc_hash}, cached {cached_hash}); refresh discinfo to proceed",
        )
    mp = cached.get("mount_point")
    if mp and str(mp) != str(mount_point):
        raise HTTPException(status_code=409, detail="Requested mount point does not match cached disc")
    return cached


@_internal_only()
def scan_disc_info(disc_num: str, mount_point: str) -> dict:
    """
    Run info scan (with lock check).
    Internal use only - called by disc_manager.
    """
    log.info(f"scan_disc_info disc_num={disc_num} mount_point={mount_point}")
    
    # Check if operation is active
    # Lock by mount_point (stable per-drive identity, #542). disc_num is the
    # MakeMKV-side volatile index which renumbers across hot-plug.
    if is_operation_active(mount_point, OPERATION_RIP):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot scan: rip operation in progress for disc {disc_num}"
        )

    lock = acquire_operation_lock(mount_point, OPERATION_INFO)
    if lock is None:
        raise HTTPException(
            status_code=409,
            detail=f"Info scan already in progress for disc {disc_num}"
        )

    try:
        return _load_discinfo(disc_num, mount_point, refresh=True, source="disc_manager_scan")
    finally:
        release_operation_lock(lock)


@_internal_only()
def hash_disc(disc_num: str, mount_point: str) -> dict:
    """
    Calculate hash (with lock check).
    Internal use only - called by disc_manager.
    """
    log.info(f"hash_disc disc_num={disc_num} mount_point={mount_point}")
    
    # Lock by mount_point (stable per-drive identity, #542).
    if is_operation_active(mount_point, OPERATION_RIP):
        raise HTTPException(
            status_code=409,
            detail=f"Cannot hash: rip operation in progress for disc {disc_num}"
        )

    lock = acquire_operation_lock(mount_point, OPERATION_HASH)
    if lock is None:
        raise HTTPException(
            status_code=409,
            detail=f"Hash operation already in progress for disc {disc_num}"
        )
    
    try:
        # Calculate hash
        content_hash = hash_media_disc(mount_point, allow_reentrant=False)
        
        # Return hash
        return {
            "disc_num": str(disc_num),
            "mount_point": mount_point,
            "disc_hash": content_hash,
            "content_hash": content_hash,
        }
    finally:
        release_operation_lock(lock)


@_internal_only()
def handle_disc_eject(disc_num: str) -> dict:
    """
    Mark disc as ejected (clears cache).
    Returns disc_hash if available in cache before clearing.
    Internal use only - called by UDS server or disc_manager.
    """
    log.info(f"handle_disc_eject disc_num={disc_num}")
    try:
        # Get disc_hash from cache before clearing (if available)
        disc_hash = None
        cached = cache_get(str(disc_num))  # disc_num is all we have here
        mount_point = None
        if cached:
            disc_hash = cached.get("disc_hash") or cached.get("content_hash")
            mount_point = cached.get("mount_point")

        if mount_point:
            return handle_disc_eject_for_device(mount_point, udev_disc_num=str(disc_num))

        clear_key(str(disc_num))
        mark_slot_absent(str(disc_num))
        log.info(f"Cleared cache for disc {disc_num} (ejected)")
        result = {"status": "ok", "message": "Cache cleared"}
        if disc_hash:
            result["disc_hash"] = disc_hash
        return result
    except Exception as exc:
        log.error(f"Error ejecting disc {disc_num}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@_internal_only()
def handle_disc_eject_for_device(mount_point: str, udev_disc_num: Optional[str] = None) -> dict:
    """
    Eject/clear disc cache using the block device path (udev DEVNAME).

    Resolves disc_hash via get_by_mount_point first so the MakeMKV cache key (e.g. "0")
    does not have to match udev's srN-derived disc_num (e.g. "2").
    """
    from core.utils import kill_makemkvcon_for_disc, _find_makemkvcon_process_for_disc

    log.info(
        "handle_disc_eject_for_device mount_point=%s udev_disc_num=%s",
        mount_point,
        udev_disc_num,
    )
    cached = get_by_mount_point(mount_point) if mount_point else None
    if not cached and udev_disc_num:
        cached = cache_get(str(udev_disc_num))

    disc_hash = None
    canonical = None
    if cached:
        disc_hash = cached.get("disc_hash") or cached.get("content_hash")
        canonical = cached.get("disc_num")

    kill_ids: list[str] = []
    if mount_point:
        kill_ids.append(mount_point)
    if canonical and str(canonical) not in kill_ids:
        kill_ids.append(str(canonical))
    if udev_disc_num and str(udev_disc_num) not in kill_ids:
        kill_ids.append(str(udev_disc_num))

    for kid in kill_ids:
        try:
            kill_makemkvcon_for_disc(kid)
        except Exception as kill_exc:
            log.warning("kill_makemkvcon_for_disc failed for %s: %s", kid, kill_exc)

    makemkv_running = False
    for kid in kill_ids:
        pid, _ = _find_makemkvcon_process_for_disc(kid)
        if pid:
            makemkv_running = True
            break

    if not makemkv_running and mount_point:
        clear_keys_by_mount_point(mount_point)
    elif not makemkv_running and udev_disc_num:
        clear_key(str(udev_disc_num))

    # Mark slot absent by mount_point (stable physical identity)
    if mount_point:
        mark_slot_absent(mount_point)

    result: dict = {"status": "ok", "message": "Cache cleared"}
    if disc_hash:
        result["disc_hash"] = disc_hash
    if canonical:
        result["canonical_disc_num"] = str(canonical)
    log.info(
        "handle_disc_eject_for_device done mount_point=%s disc_hash=%s canonical=%s",
        mount_point,
        "set" if disc_hash else None,
        canonical,
    )
    return result


@_internal_only()
def handle_disc_insert(disc_num: str, mount_point: str) -> dict:
    """
    Handle disc insertion: perform full scan sequence and notify Disc Manager.
    
    Flow:
    1. Immediately notify Disc Manager (early notification for "Loading Disc Info...")
    2. Clear cache
    3. Run hash_media_disc to get content_hash
    4. Run info dev:{mount_point}; parse MakeMKV index from DRV and upsert _last_drive_scan for this path
    5. Notify Disc Manager with raw_data (completion notification)
    
    Internal use only - called by UDS server handler.
    
    Args:
        disc_num: Disc number (may be "9999" if not yet identified)
        mount_point: Mount point (e.g., "/dev/sr1")
    
    Returns:
        dict with status and message
    """
    func_logger = get_logger("core._drive_operations", "handle_disc_insert")
    func_logger.debug("handle_disc_insert called disc_num=%s mount_point=%s", disc_num, mount_point)
    log.info(f"handle_disc_insert disc_num={disc_num} mount_point={mount_point}")

    if not try_begin_insert_scan(mount_point):
        log.info(
            "handle_disc_insert: insert scan already in progress for mount_point=%s, skipping",
            mount_point,
        )
        return {
            "status": "ok",
            "message": "Insert scan already in progress",
            "skipped_scan_in_progress": True,
            "disc_num": str(disc_num),
            "mount_point": mount_point,
        }

    # Import here to avoid circular dependency
    from core import disc_manager

    try:
        # #562 PR 4: removed pre-flight ``ensure_makemkv_index_for_mount(
        # refresh_enumeration_first=True)`` — the ``disc:9999`` enumeration
        # raced any concurrent ``mkv dev:`` on a sibling drive and emitted
        # MSG:5010. The per-disc ``info dev:`` scan run by the rest of this
        # handler refreshes path→DRV index for THIS device on its own.

        # IMMEDIATE: Notify Disc Manager of insertion (before any scanning)
        try:
            disc_manager.on_disc_inserted(disc_num, mount_point)
            log.info(f"Notified Disc Manager of disc insertion: disc_num={disc_num}")
        except Exception as notify_exc:
            log.warning(f"Failed to notify Disc Manager of insertion: {notify_exc}")
        
        # Clear cache by mount_point (stable physical identity)
        clear_keys_by_mount_point(mount_point)
        log.info(f"Invalidated cache for mount_point={mount_point} (inserted)")
        
        log.info(f"Starting scan sequence for mount_point={mount_point}")
        
        # 1. Calculate hash
        content_hash = None
        try:
            log.info(f"Calculating hash for {mount_point}")
            content_hash = hash_media_disc(mount_point, allow_reentrant=False)
            log.info(f"Hash calculated: {content_hash}")
        except Exception as hash_exc:
            log.error(f"Failed to calculate hash: {hash_exc}")
            # Continue without hash (Disc Manager can handle it)
        
        # 2. Run makemkv info scan (always dev:{mount} so index matches this device; upsert drive cache)
        info_log = None
        extracted_disc_num = None
        extracted_disc_name = None
        try:
            log.info(f"Running makemkv info scan for {mount_point}")
            min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
            info_args = f"info dev:{mount_point} -r --minlength={min_title_len}"
            info_log, _ = run_makemkv(info_args)
            il_text = str(info_log) if info_log is not None else ""
            parsed_idx, parsed_hw, vol_label = parse_drv_fields_for_mount(il_text, mount_point)
            if parsed_idx:
                upsert_makemkv_drive_cache_for_mount(mount_point, parsed_idx, parsed_hw)
                extracted_disc_num = parsed_idx
                extracted_disc_name = vol_label
                log.info(
                    "Extracted disc_num=%s disc_name=%s from info output for %s",
                    extracted_disc_num,
                    extracted_disc_name,
                    mount_point,
                )
            log.info(f"Info scan completed for {mount_point}")
        except Exception as info_exc:
            log.error(f"Failed to run info scan: {info_exc}")
            # Continue without info_log (Disc Manager can handle it)
        
        # 3. Build raw_data dict
        # Use extracted disc_num if available, otherwise use mount_point identifier
        # Store MakeMKV disc name separately from release metadata disc_name
        raw_data = {
            "disc_num": str(extracted_disc_num) if extracted_disc_num else str(disc_num),
            "mount_point": mount_point,
        }
        # Add makemkv_disc_name if extracted from DRV line (for drive selection, not labeling)
        if extracted_disc_name:
            raw_data["makemkv_disc_name"] = extracted_disc_name
        if content_hash:
            raw_data["disc_hash"] = content_hash
            raw_data["content_hash"] = content_hash
        if info_log:
            raw_data["info_log"] = info_log if isinstance(info_log, str) else "\n".join(info_log) if isinstance(info_log, list) else str(info_log)
            raw_data["raw_info_log"] = raw_data["info_log"]
        
        # Dump info log if debug enabled
        _maybe_dump_info_log(raw_data.get("raw_info_log"), raw_data.get("disc_num", disc_num), content_hash)
        
        # 4. Notify Disc Manager of scan completion
        try:
            disc_manager.on_disc_scan_complete(raw_data)
            log.info(f"Notified Disc Manager of scan completion: disc_num={raw_data.get('disc_num')} mount_point={mount_point}")
        except Exception as notify_exc:
            log.error(f"Failed to notify Disc Manager of scan completion: {notify_exc}")
            # Don't fail the whole operation if notification fails
        
        canonical = str(raw_data.get("disc_num", disc_num))
        # Mark slot stable by mount_point (stable physical identity)
        mark_slot_stable(mount_point)
        return {
            "status": "ok",
            "message": "Disc scan completed",
            "disc_num": canonical,
            "mount_point": mount_point,
        }
    except Exception as exc:
        log.error(f"Error handling disc insert for disc_num={disc_num} mount_point={mount_point}: {exc}")
        # Try to notify Disc Manager of failure
        try:
            from api.routers.events import _notify_disc_scan_complete
            error_payload = {
                "disc_num": str(disc_num),
                "mount_point": mount_point,
                "error": str(exc),
                "type": "disc_error",
            }
            _notify_disc_scan_complete(error_payload)
        except Exception:
            pass  # Ignore notification errors
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    finally:
        end_insert_scan(mount_point)

