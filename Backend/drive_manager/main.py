"""
Standalone drive manager service.
Owns drive enumeration and disc metadata loading so other processes do not
touch the optical drive directly. Provides a small FastAPI surface that the
main backend can proxy to.

INTERNAL USE ONLY: This service should not be directly accessible from the frontend.
All disc operations should go through the main Backend API -> Disc Manager -> Drive Manager.
"""
import logging
import os
import stat
import time
from typing import List, Tuple, Optional, Callable
from time import perf_counter
from pydantic import BaseModel

from fastapi import FastAPI, HTTPException
from contextlib import asynccontextmanager

from core.disc_cache import get as cache_get, set_payload as cache_set, clear_key, clear_keys_by_mount_point
from core.utils import (
    get_drives,
    MakeMKVError,
    hash_media_disc,
    run_makemkv,
    build_drive_api_dict,
    parse_drv_fields_for_mount,
    upsert_makemkv_drive_cache_for_mount,
    ensure_makemkv_index_for_mount,
)
from pathlib import Path
from drive_manager.state import get_drive_state
import requests

try:
    from .uds_server import UDSServer, get_socket_path
except ImportError:
    # Fallback for when running as script (e.g., via uvicorn)
    import sys
    from pathlib import Path
    # Add parent directory to path to allow importing from core
    parent_dir = str(Path(__file__).parent.parent)
    if parent_dir not in sys.path:
        sys.path.insert(0, parent_dir)
    from drive_manager.uds_server import UDSServer, get_socket_path

from core.logging_utils import get_logger, _get_log_level_from_env
log = logging.getLogger("drive_manager")
_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
_LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"

# Global UDS server instance
_uds_server: Optional[UDSServer] = None

# Global callback for progress updates (set by backend)
_progress_callback: Optional[Callable[[str, dict], None]] = None


def _read_sys_block_size(device_name: str, sys_block_root: Path = Path("/sys/class/block")) -> int | None:
    size_path = sys_block_root / device_name / "size"
    try:
        if size_path.exists():
            sectors = int(size_path.read_text().strip())
            if sectors > 0:
                return sectors * 512
    except Exception:
        return None
    return None


def _block_device_name_from_mount_point(mount_point: str) -> str | None:
    """
    Resolve mount_point to the block device name (e.g. sr0, sr1) for use with
    /sys/class/block/<name>/size. Uses only mount_point; does not use disc_num.
    """
    try:
        resolved = Path(mount_point).resolve()
    except Exception:
        return None

    # Block device path (e.g. /dev/sr1)
    try:
        if os.path.exists(mount_point):
            st = os.stat(mount_point)
            if stat.S_ISBLK(st.st_mode):
                return resolved.name
    except OSError:
        pass

    # Directory or path: find device from /proc/mounts (longest matching mount)
    try:
        with open("/proc/mounts", "r") as f:
            lines = f.readlines()
    except OSError:
        return None

    target = str(resolved)
    best_dev = None
    best_len = -1
    for line in lines:
        parts = line.split()
        if len(parts) < 2:
            continue
        dev, mnt = parts[0], parts[1].replace("\\040", " ")
        try:
            mnt_resolved = str(Path(mnt).resolve())
        except Exception:
            continue
        if target == mnt_resolved or (mnt_resolved and target.startswith(mnt_resolved + os.sep)):
            if len(mnt_resolved) > best_len:
                best_len = len(mnt_resolved)
                best_dev = dev
    if not best_dev:
        return None

    dev_name = Path(best_dev).name
    sys_block = Path("/sys/class/block") / dev_name
    if sys_block.exists():
        try:
            real = os.path.realpath(sys_block)
            dev_name = Path(real).name
        except OSError:
            pass
    return dev_name


def _get_disc_size_bytes(mount_point: str) -> int | None:
    """
    Return disc capacity in bytes for the given mount point, or None if
    unavailable. Resolves mount_point to the block device and reads
    /sys/class/block/<device>/size (sectors * 512).
    """
    device_name = _block_device_name_from_mount_point(mount_point)
    if not device_name:
        return None
    return _read_sys_block_size(device_name)


def _configure_logging():
    """Ensure drive_manager and uvicorn logs include timestamps."""
    # Get log level from environment
    log_level = _get_log_level_from_env()
    
    # If uvicorn already configured handlers, just update their formatters.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "drive_manager"):
        logger = logging.getLogger(name)
        logger.setLevel(log_level)
        for handler in logger.handlers:
            handler.setFormatter(logging.Formatter(_LOG_FORMAT, _LOG_DATEFMT))
            handler.setLevel(log_level)

    # If no handlers exist (unlikely under uvicorn), add a root handler.
    if not logging.getLogger().handlers:
        logging.basicConfig(level=log_level, format=_LOG_FORMAT, datefmt=_LOG_DATEFMT)


_configure_logging()


def _handle_udev_event(action: str, device: str, disc_num: Optional[str] = None) -> dict:
    """
    Handle udev event (insert/eject).
    
    Args:
        action: "insert" or "eject"
        device: Device path (e.g., "/dev/sr1")
        disc_num: Optional disc number
    
    Returns:
        Dict with status and message
    """
    try:
        if action == "eject":
            # Notify Backend first so it can read cache, fail jobs, revoke Celery, and kill makemkvcon
            disc_num_resolved = disc_num
            if not disc_num_resolved:
                import re
                m = re.search(r"sr(\d+)$", device)
                if m:
                    disc_num_resolved = m.group(1)
            try:
                base = _backend_base_url()
                eject_url = f"{base}/events/drive/eject"
                resp = requests.post(eject_url, data={"device": device}, timeout=5.0)
                if resp.status_code != 200:
                    log.warning("Backend eject notification returned %s: %s", resp.status_code, resp.text[:200])
                else:
                    log.info("Notified Backend of eject: device=%s disc_num=%s", device, disc_num_resolved)
            except Exception as notify_exc:
                log.warning("Failed to notify Backend of eject: %s", notify_exc)
            # Then clear local cache — by device (mount_point, stable physical identity)
            if device:
                clear_keys_by_mount_point(device)
                log.info(f"Cleared cache for device={device} (ejected)")
            elif disc_num_resolved:
                clear_key(str(disc_num_resolved))
                log.info(f"Cleared cache for disc {disc_num_resolved} (ejected)")
            return {"status": "ok", "message": "Cache cleared"}

        elif action == "insert":
            # Invalidate cache (will be refreshed on next request)
            # Use device (mount_point) as primary key for cache clearing
            if device:
                clear_keys_by_mount_point(device)
                log.info(f"Invalidated cache for device={device} (inserted)")
            elif disc_num:
                clear_key(str(disc_num))
                log.info(f"Invalidated cache for disc {disc_num} (inserted)")
            return {"status": "ok", "message": "Cache invalidated"}
        
        else:
            return {"status": "error", "message": f"Unknown action: {action}"}
    except Exception as exc:
        log.error(f"Error handling udev event: {exc}")
        return {"status": "error", "message": str(exc)}


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown lifecycle."""
    global _uds_server
    # Start UDS server
    _uds_server = UDSServer(_handle_udev_event)
    _uds_server.start()
    log.info("Drive Manager started with UDS server")
    
    yield
    
    # Shutdown UDS server
    if _uds_server:
        _uds_server.stop()
        _uds_server = None
    log.info("Drive Manager stopped")


app = FastAPI(title="Drive Manager", version="0.1.0", lifespan=lifespan)


def _tuple_drives(drives: List[Tuple[str, str]]) -> list:
    return [build_drive_api_dict(num, mp) for num, mp in drives]


BACKEND_CACHE_URL = os.getenv("MKVAUTO_BACKEND_CACHE_URL", "http://127.0.0.1:8000/discs/cache")


def _backend_base_url() -> str:
    """Base URL for Backend API (e.g. http://127.0.0.1:8000). Used for eject notification."""
    url = os.getenv("MKVAUTO_BACKEND_URL", "").strip()
    if url:
        return url.rstrip("/")
    from urllib.parse import urlparse
    parsed = urlparse(BACKEND_CACHE_URL)
    return f"{parsed.scheme}://{parsed.netloc}"
INFO_LOG_DEBUG = os.getenv("MKVAUTO_DEBUG_INFO_LOG", "").lower() in ("1", "true", "yes")
INFO_LOG_DIR = os.getenv(
    "MKVAUTO_INFO_LOG_DIR",
    os.path.join(os.getenv("MKVAUTO_ROOT", "/tmp/MakeMKV-Auto"), "logs", "info_logs"),
)


def _maybe_dump_info_log(info_log: str | list[str] | None, disc_num: str, disc_hash: str | None) -> None:
    """Optionally dump info log to file for debugging."""
    if not INFO_LOG_DEBUG or not info_log:
        return
    try:
        os.makedirs(INFO_LOG_DIR, exist_ok=True)
        filename = f"info_{disc_num}_{disc_hash or 'unknown'}.log"
        filepath = os.path.join(INFO_LOG_DIR, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            if isinstance(info_log, list):
                f.write("\n".join(info_log))
            else:
                f.write(info_log)
        log.debug("Dumped info log to %s", filepath)
    except Exception as exc:
        log.debug("Failed to dump info log: %s", exc)


def _push_backend_cache(payload: dict) -> None:
    """Push disc info to backend cache endpoint."""
    try:
        requests.post(BACKEND_CACHE_URL, json=payload, timeout=5.0)
    except Exception as exc:
        log.debug("Failed to push cache to backend: %s", exc)


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/drives")
def drives():
    """
    List available optical drives.
    """
    try:
        drives_list = get_drives()
        log.info("Drives enumerated: %s", drives_list)
        return _tuple_drives(drives_list)
    except MakeMKVError as exc:
        log.warning("Drive scan failed: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        log.exception("Drive scan failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


def _load_discinfo(disc_num: str, mount_point: str, refresh: bool = False, source: str = "unspecified") -> dict:
    """
    Internal helper to fetch disc info, optionally bypassing cache.
    """
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

    log.info("discinfo scan start disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    
    # CRITICAL: Check if drive is busy (rip/scan in progress) - if so, skip scan and return cached data
    drive_state = get_drive_state()
    logger = get_logger("drive_manager.main", "_load_discinfo")
    logger.debug("Checking drive state disc_num=%s mount_point=%s refresh=%s source=%s", disc_num, mount_point, refresh, source)
    is_busy = drive_state.is_drive_busy(disc_num, mount_point)
    logger.debug("Drive state check result disc_num=%s mount_point=%s is_busy=%s", disc_num, mount_point, is_busy)
    if is_busy:
        active_op = drive_state.get_operation(disc_num, mount_point)
        logger.debug("Drive is busy disc_num=%s mount_point=%s active_op_type=%s active_job_id=%s", 
                    disc_num, mount_point, active_op.operation_type if active_op else None, active_op.job_id if active_op else None)
        log.warning("Drive busy for disc %s - %s in progress, skipping scan", disc_num, active_op.operation_type if active_op else "operation")
        cached = cache_get(mount_point) if mount_point else None
        if not cached:
            cached = cache_get(str(disc_num))
        if cached:
            log.info("Returning cached discinfo for disc %s (drive busy)", disc_num)
            return cached
        # No cache available - raise error to indicate scan cannot proceed
        raise HTTPException(
            status_code=409,
            detail=f"Drive scan cannot proceed: {active_op.operation_type if active_op else 'operation'} in progress for disc {disc_num}. Use cached data or wait for operation to complete."
        )
    
    started = perf_counter()
    
    # Drive Manager only returns raw info (hash + makemkv info log)
    # No DiscDB queries, no parsing - that's handled by Disc Manager
    logger.debug("About to start scan operation disc_num=%s mount_point=%s source=%s", disc_num, mount_point, source)
    # Mark drive as busy for info scan
    drive_state.start_operation(disc_num, mount_point, "scan")
    logger.debug("Scan operation started in drive state disc_num=%s mount_point=%s", disc_num, mount_point)
    try:
        ensure_makemkv_index_for_mount(mount_point, refresh_enumeration_first=True)
        # 1. Calculate hash
        log.info("Calculating hash for disc %s", disc_num)
        logger.debug("About to calculate hash disc_num=%s mount_point=%s", disc_num, mount_point)
        # Mark drive as busy for hash operation
        drive_state.end_operation(disc_num, mount_point)  # End scan, start hash
        drive_state.start_operation(disc_num, mount_point, "hash")
        try:
            content_hash = hash_media_disc(mount_point, allow_reentrant=False)
        finally:
            drive_state.end_operation(disc_num, mount_point)  # End hash
        log.info("Hash calculated: %s", content_hash)
        
        # 2. Run makemkv info scan
        log.info("Running makemkv info scan for disc %s", disc_num)
        logger.debug("About to run makemkvcon disc_num=%s mount_point=%s", disc_num, mount_point)
        drive_state.end_operation(disc_num, mount_point)  # End hash
        drive_state.start_operation(disc_num, mount_point, "scan")
        try:
            min_title_len = int(os.getenv("MKVAUTO_MIN_TITLE_LENGTH", "0"))
            info_args = f"info dev:{mount_point} -r --minlength={min_title_len}"
            logger.debug("Calling run_makemkv disc_num=%s mount_point=%s info_args=%s", disc_num, mount_point, info_args)
            info_log, _ = run_makemkv(info_args)
            logger.debug("makemkvcon completed disc_num=%s info_log_length=%s", disc_num, len(info_log) if isinstance(info_log, (str, list)) else 0)
        finally:
            drive_state.end_operation(disc_num, mount_point)  # End scan
        
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

        # 3. Return raw payload (hash + info log only)
        disc_size_bytes = _get_disc_size_bytes(mount_point)
        payload = {
            "disc_num": canonical,
            "mount_point": mount_point,
            "disc_hash": content_hash,
            "content_hash": content_hash,
            "info_log": il_text,
            "raw_info_log": il_text,
        }
        if disc_size_bytes:
            payload["disc_size_bytes"] = disc_size_bytes
        
        _maybe_dump_info_log(payload.get("raw_info_log"), canonical, content_hash)
        # Cache by mount_point (primary key for multi-drive correctness)
        cache_set(mount_point, payload)
        _push_backend_cache(payload)
        
        log.info("discinfo scan completed: hash=%s info_log_length=%s", content_hash, len(payload.get("info_log", "")))
        return payload
    except Exception as exc:
        log.error("Error during discinfo scan: %s", exc)
        # Ensure operation is marked as complete on error
        drive_state.end_operation(disc_num, mount_point)
        raise
    finally:
        # Ensure operation is marked as complete even on success
        drive_state.end_operation(disc_num, mount_point)
        elapsed = perf_counter() - started
        log.info("discinfo scan end disc_num=%s mount_point=%s source=%s elapsed=%.2fs", disc_num, mount_point, source, elapsed)


@app.get("/discinfo")
def discinfo(disc_num: str, mount_point: str, refresh: bool = False):
    """
    Load disc metadata (hash + DiscDB lookup + track mapping).
    Caches results keyed by disc_num (and disc_hash when present).
    """
    source = "ui_refresh" if refresh else "auto_or_cache"
    log.info("GET /discinfo disc_num=%s mount_point=%s refresh=%s source=%s", disc_num, mount_point, refresh, source)
    try:
        return _load_discinfo(disc_num, mount_point, refresh=refresh, source=source)
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Error in discinfo endpoint")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/discinfo/refresh")
def discinfo_refresh(disc_num: str, mount_point: str):
    """
    Force a rescan of the disc, bypassing cache.
    """
    log.info("POST /discinfo/refresh disc_num=%s mount_point=%s", disc_num, mount_point)
    try:
        return _load_discinfo(disc_num, mount_point, refresh=True, source="refresh_endpoint")
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Error in discinfo refresh endpoint")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/discinfo/validate")
def discinfo_validate(disc_num: str, mount_point: str, disc_hash: str):
    """
    Validate cached disc info against an expected hash without triggering a rescan.
    """
    log.info("POST /discinfo/validate disc_num=%s mount_point=%s disc_hash=%s", disc_num, mount_point, disc_hash)
    cached = cache_get(mount_point) if mount_point else None
    if not cached:
        cached = cache_get(str(disc_num))
    if not cached:
        raise HTTPException(status_code=404, detail="Disc info not cached")
    cached_hash = cached.get("disc_hash") or cached.get("content_hash")
    if cached_hash != disc_hash:
        raise HTTPException(status_code=409, detail=f"Hash mismatch: cached={cached_hash}, expected={disc_hash}")
    return cached


@app.post("/disc/eject")
def disc_eject(disc_num: str):
    """
    Handle disc ejection (clear cache).
    """
    log.info("POST /disc/eject disc_num=%s", disc_num)
    try:
        clear_key(str(disc_num))
        log.info(f"Cleared cache for disc {disc_num} (ejected)")
        return {"status": "ok", "message": "Cache cleared"}
    except Exception as exc:
        log.error(f"Error ejecting disc {disc_num}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/disc/insert")
def disc_insert(disc_num: str):
    """
    Handle disc insertion (invalidate cache).
    """
    log.info("POST /disc/insert disc_num=%s", disc_num)
    try:
        clear_key(str(disc_num))
        log.info(f"Invalidated cache for disc {disc_num} (inserted)")
        return {"status": "ok", "message": "Cache invalidated"}
    except Exception as exc:
        log.error(f"Error inserting disc {disc_num}: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/discinfo/scan")
def discinfo_scan(disc_num: str, mount_point: str):
    """
    Run info scan (with state check).
    Internal endpoint - called by Disc Manager.
    """
    log.info(f"POST /discinfo/scan disc_num={disc_num} mount_point={mount_point}")
    
    # Check if drive is busy
    drive_state = get_drive_state()
    if drive_state.is_drive_busy(disc_num, mount_point):
        active_op = drive_state.get_operation(disc_num, mount_point)
        raise HTTPException(
            status_code=409,
            detail=f"Cannot scan: {active_op.operation_type if active_op else 'operation'} in progress for disc {disc_num}"
        )
    
        return _load_discinfo(disc_num, mount_point, refresh=True, source="disc_manager_scan")


@app.post("/discinfo/hash")
def discinfo_hash(disc_num: str, mount_point: str):
    """
    Calculate hash (with state check).
    Internal endpoint - called by Disc Manager.
    """
    log.info(f"POST /discinfo/hash disc_num={disc_num} mount_point={mount_point}")
    
    # Check if drive is busy
    drive_state = get_drive_state()
    if drive_state.is_drive_busy(disc_num, mount_point):
        active_op = drive_state.get_operation(disc_num, mount_point)
        raise HTTPException(
            status_code=409,
            detail=f"Cannot hash: {active_op.operation_type if active_op else 'operation'} in progress for disc {disc_num}"
        )
    
    # Mark drive as busy for hash operation
    drive_state.start_operation(disc_num, mount_point, "hash")
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
        drive_state.end_operation(disc_num, mount_point)


class RipRequest(BaseModel):
    """Request model for rip endpoint."""
    job_id: str
    disc_num: str
    mount_point: str
    mode: str = "copy"  # "copy" or "backup"
    output_dir: str
    progress_callback_url: Optional[str] = None  # URL to POST progress updates to


# NOTE: The /rip endpoint has been removed. All rip operations must go through
# the Backend API /jobs/rip endpoint, which uses DriveGatekeeper as the single
# entry point for all rip operations. This ensures proper duplicate prevention
# and state management via Postgres.


@app.get("/state")
def get_state():
    """
    Get current drive state (for debugging/monitoring).
    """
    drive_state = get_drive_state()
    operations = drive_state.get_active_operations()
    return {
        "active_operations": {
            key: {
                "disc_num": op.disc_num,
                "mount_point": op.mount_point,
                "operation_type": op.operation_type,
                "job_id": op.job_id,
                "started_at": op.started_at,
            }
            for key, op in operations.items()
        }
    }


@app.get("/state/job/{job_id}")
def get_state_by_job_id(job_id: str):
    """
    Get active operation for a specific job_id, if any.
    Used by backend API to verify if a job is actually running.
    """
    drive_state = get_drive_state()
    operation = drive_state.get_operation_by_job_id(job_id)
    if operation:
        return {
            "active": True,
            "disc_num": operation.disc_num,
            "mount_point": operation.mount_point,
            "operation_type": operation.operation_type,
            "job_id": operation.job_id,
            "started_at": operation.started_at,
        }
    return {"active": False}


@app.get("/state/check")
def check_drive_state(disc_num: str, mount_point: str):
    """
    Check if a drive is busy.
    """
    drive_state = get_drive_state()
    is_busy = drive_state.is_drive_busy(disc_num, mount_point)
    active_op = drive_state.get_operation(disc_num, mount_point) if is_busy else None
    return {
        "is_busy": is_busy,
        "active_operation": {
            "operation_type": active_op.operation_type,
            "job_id": active_op.job_id,
            "started_at": active_op.started_at,
        } if active_op else None,
    }


@app.post("/state/start")
def start_operation(disc_num: str, mount_point: str, operation_type: str, job_id: str | None = None):
    """
    Mark a drive as busy with an operation.
    """
    drive_state = get_drive_state()
    if drive_state.is_drive_busy(disc_num, mount_point):
        active_op = drive_state.get_operation(disc_num, mount_point)
        raise HTTPException(
            status_code=409,
            detail=f"Drive already busy: {active_op.operation_type if active_op else 'operation'} in progress"
        )
    drive_state.start_operation(disc_num, mount_point, operation_type, job_id=job_id)
    return {"status": "ok", "message": f"Operation {operation_type} started"}


@app.post("/state/end")
def end_operation(disc_num: str, mount_point: str):
    """
    Mark a drive operation as complete.
    """
    drive_state = get_drive_state()
    drive_state.end_operation(disc_num, mount_point)
    return {"status": "ok", "message": "Operation ended"}
