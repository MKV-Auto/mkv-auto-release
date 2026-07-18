"""
Unified locking system for disc operations.
Manages operation-specific locks (hash, info, rip) per disc with process detection.
"""
import logging
import json
import time
import os
import subprocess
try:
    import psutil  # type: ignore
except Exception:
    from types import SimpleNamespace
    psutil = SimpleNamespace(process_iter=None)  # type: ignore
from pathlib import Path
from typing import Any, Optional, List
from filelock import FileLock, Timeout

from core.utils import get_mkvauto_tmp, MAKEMKVCON_PATH

logger = logging.getLogger(__name__)

# Operation types
OPERATION_HASH = "hash"
OPERATION_INFO = "info"
OPERATION_RIP = "rip"

# Lock timeout (seconds)
LOCK_TIMEOUT = 1.0


def _sanitize_lock_key(key: str) -> str:
    """Sanitize a key (mount_point or disc_num) for use in lock file names.

    ``/dev/sr0`` → ``dev_sr0``; plain digit strings pass through.
    """
    return key.replace("/", "_").replace("\\", "_").strip("_")


def get_operation_lock_path(key: str, operation_type: str) -> Path:
    """
    Get the lock file path for a specific disc operation.

    *key* is the **mount_point** (preferred, e.g. ``/dev/sr0``) or legacy
    ``disc_num``.  The file name is sanitized so ``/dev/sr0`` becomes
    ``dev_sr0.rip.lock``.
    """
    tmp_dir = get_mkvauto_tmp()
    lock_dir = tmp_dir / "disc_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    safe = _sanitize_lock_key(key)
    return lock_dir / f"{safe}.{operation_type}.lock"


def is_operation_active(key: str, operation_type: str, mount_point: str | None = None) -> bool:
    """
    Check if an operation is currently active for a disc.
    Checks both lock file existence and process list.

    *key* is the primary lookup (mount_point preferred).  *mount_point* is an
    optional secondary hint used in process detection when *key* is a disc_num.
    """
    # Check lock file
    lock_path = get_operation_lock_path(key, operation_type)
    if lock_path.exists():
        try:
            test_lock = FileLock(lock_path, timeout=0.1)
            test_lock.acquire()
            test_lock.release()
            # Lock was available, so it's stale
            try:
                lock_path.unlink()
            except Exception:
                pass
            # fall through to process check
        except Timeout:
            # Lock is held, operation is active
            return True

    # Also check legacy disc_num lock path if key looks like a mount_point
    if key.startswith("/dev/"):
        # key is a mount_point — no legacy disc_num lock to check
        pass
    else:
        # key might be a disc_num; no extra check needed
        pass

    # Check process list — search for both dev:{mount_point} and disc:{disc_num}
    mp = mount_point or (key if key.startswith("/dev/") else None)
    disc_num = key if not key.startswith("/dev/") else None
    active = _is_makemkvcon_running_for_operation(
        operation_type, mount_point=mp, disc_num=disc_num,
    )
    return active


def _is_makemkvcon_running_for_operation(
    operation_type: str,
    *,
    mount_point: str | None = None,
    disc_num: str | None = None,
) -> bool:
    """
    Check if a makemkvcon process is running for the given disc and operation type.

    Searches for ``dev:{mount_point}`` patterns first (preferred since multi-drive
    fix), then ``disc:{disc_num}`` for backward compatibility with any in-flight
    processes started before the change.
    """
    # Build search patterns
    patterns: list[str] = []
    if mount_point:
        mp_esc = mount_point.replace("/", r"\/")  # escape for regex
        patterns.append(f"dev:{mount_point}")
    if disc_num:
        patterns.append(f"disc:{disc_num}")

    if not patterns:
        return False

    def _matches_operation(cmd_tokens: list[str]) -> bool:
        if operation_type == OPERATION_RIP:
            return any(t in ("mkv", "backup") for t in cmd_tokens)
        elif operation_type == OPERATION_INFO:
            return "info" in cmd_tokens and not any(t in ("mkv", "backup") for t in cmd_tokens)
        elif operation_type == OPERATION_HASH:
            return "info" in cmd_tokens and not any(t in ("mkv", "backup") for t in cmd_tokens)
        return False

    try:
        if psutil is None or not getattr(psutil, "process_iter", None):
            raise ImportError

        for proc in psutil.process_iter(['pid', 'cmdline']):
            try:
                cmdline = proc.cmdline()
                if not cmdline:
                    continue

                cmdline_str = " ".join(cmdline)
                cmd_tokens = [str(token) for token in cmdline]

                # Must be a makemkvcon process
                if MAKEMKVCON_PATH not in cmdline_str and "makemkvcon" not in cmdline_str.lower():
                    continue

                # Check if any of our patterns match
                if not any(pat in cmdline_str for pat in patterns):
                    continue

                if _matches_operation(cmd_tokens):
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
    except ImportError:
        # Fallback to pgrep if psutil is not available
        try:
            for pat in patterns:
                if operation_type == OPERATION_RIP:
                    result = subprocess.run(
                        ["pgrep", "-f", f"makemkvcon.*(mkv|backup).*{pat}"],
                        capture_output=True, timeout=1,
                    )
                else:  # INFO or HASH
                    result = subprocess.run(
                        ["pgrep", "-f", f"makemkvcon.*info.*{pat}"],
                        capture_output=True, timeout=1,
                    )
                if result.returncode == 0:
                    return True
        except Exception as exc:
            logger.debug(f"Error checking process list: {exc}")
            return False
    except Exception as exc:
        logger.error(f"Error checking for running makemkvcon process: {exc}")
        return False

    return False


def get_active_operations(key: str, mount_point: str | None = None) -> List[str]:
    """
    Get list of active operations for a disc.

    *key* is the primary lookup (mount_point preferred).  *mount_point* is passed
    through to ``is_operation_active`` for process detection.
    """
    active = []
    for op_type in [OPERATION_HASH, OPERATION_INFO, OPERATION_RIP]:
        if is_operation_active(key, op_type, mount_point=mount_point):
            active.append(op_type)
    return active


def get_disc_lock_debug_snapshot(key: str, mount_point: str | None = None) -> dict[str, Any]:
    """
    Capture lock-file and active-operation state for one disc (logging / rip-complete debug).

    *key* is mount_point (preferred) or disc_num.  *mount_point* is passed through
    so process detection can search by device path.
    """
    lock_dir = get_mkvauto_tmp() / "disc_locks"
    snap: dict[str, Any] = {
        "key": key,
        "mount_point": mount_point,
        "lock_dir": str(lock_dir),
        "active_operations": get_active_operations(key, mount_point=mount_point),
        "lock_files": {},
    }
    for op_type in (OPERATION_HASH, OPERATION_INFO, OPERATION_RIP):
        p = get_operation_lock_path(key, op_type)
        exists = p.exists()
        held = False
        if exists:
            try:
                probe = FileLock(p, timeout=0.05)
                probe.acquire()
                probe.release()
            except Timeout:
                held = True
            except Exception:
                held = exists
        snap["lock_files"][op_type] = {"path": str(p), "exists": exists, "held": held}
    active = snap["active_operations"]
    rip_lf = snap["lock_files"].get(OPERATION_RIP) or {}
    snap["rip_lock_file_held"] = bool(rip_lf.get("held"))
    snap["duplicate_rip_suspected"] = OPERATION_RIP in active or (
        snap["rip_lock_file_held"] and OPERATION_RIP not in active
    )
    snap["other_op_blocking_rip"] = bool(active) and OPERATION_RIP not in active
    return snap


def acquire_operation_lock(
    key: str,
    operation_type: str,
    timeout: float = LOCK_TIMEOUT,
    mount_point: str | None = None,
) -> Optional[FileLock]:
    """
    Acquire a lock for a disc operation.
    Only one operation (hash, info, or rip) may run per drive at a time.

    *key* is the primary lock identifier — prefer **mount_point** (e.g.
    ``/dev/sr0``) for multi-drive correctness.  *mount_point* is an optional
    secondary hint for process detection when *key* is a disc_num.
    """
    # One operation per drive: if any operation is active, do not acquire
    active = get_active_operations(key, mount_point=mount_point)
    if active:
        logger.warning(f"Drive {key} busy (active: {active}); cannot acquire {operation_type} lock")
        return None
    # Check if this specific operation is already active (e.g. stale lock file)
    if is_operation_active(key, operation_type, mount_point=mount_point):
        logger.warning(f"Operation {operation_type} already active for drive {key}")
        return None

    # Acquire lock
    lock_path = get_operation_lock_path(key, operation_type)
    try:
        lock = FileLock(lock_path, timeout=timeout)
        lock.acquire()
        logger.debug(f"Acquired {operation_type} lock for drive {key}")
        return lock
    except Timeout:
        logger.warning(f"Timeout acquiring {operation_type} lock for drive {key}")
        return None
    except Exception as exc:
        logger.error(f"Error acquiring {operation_type} lock for drive {key}: {exc}")
        return None


def release_operation_lock(lock: Optional[FileLock]) -> None:
    """
    Release an operation lock.
    
    Args:
        lock: FileLock object to release
    """
    if lock is None:
        return
    
    try:
        lock.release()
        logger.debug("Released operation lock")
    except Exception as exc:
        logger.error(f"Error releasing lock: {exc}")




