"""
In-memory cache of disc metadata.
Maintains persistent cache entries until disc ejection/insertion.
Supports aliasing by disc_hash to avoid rescans on reconnect.
Cache is managed by Disc Manager; only Drive Manager can invalidate on insertion/removal.

Thread safety: concurrent startup insert scans (and any threaded callers) may call
set_payload concurrently; a threading.RLock protects _cache and persistence writes.
"""
import asyncio
import threading
import time
import os
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Iterable, Tuple
from core.disc import Disc
from core.logging_utils import get_logger
from core.utils import get_drives, get_mkvauto_root, get_mkvauto_tmp

logger = get_logger("core.disc_cache")

# Cache entries persist until explicitly refreshed; TTL is ignored.
TTL = None


def _compute_disk_persistence_enabled() -> bool:
    """
    Disk persistence for disc_cache is opt-in (default off).

    Stale drive_cache.json after srN / MakeMKV index changes caused wrong cache keys
    across restarts; see docs/DRIVES_DISC_CACHE_AND_MULTI_DRIVE.md.

    Set MKVAUTO_PERSIST_DISC_CACHE=1 to enable writing/reading drive_cache.json.
    MKVAUTO_DISABLE_DISC_CACHE=1 still forces persistence off (legacy).
    """
    if os.getenv("MKVAUTO_DISABLE_DISC_CACHE", "").lower() in ("1", "true", "yes"):
        return False
    return os.getenv("MKVAUTO_PERSIST_DISC_CACHE", "").lower() in ("1", "true", "yes")


# Tests may set this to False to avoid touching disk.
DISK_PERSIST_ENABLED: bool = _compute_disk_persistence_enabled()
# Async lock to prevent concurrent refreshes
_LOCK = asyncio.Lock()
# Serialize concurrent access to _cache (parallel startup rescans, SSE updates, etc.)
_cache_lock = threading.RLock()
# Internal cache: key (mount_point, disc_num, disc_hash, or disc_id) -> (timestamp, payload)
# mount_point is the *primary* key (stable physical identity); disc_num, disc_hash,
# and disc_id are secondary alias keys pointing to the same payload.
_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}
_cache_file: Path = get_mkvauto_tmp() / "drive_cache.json"

def _is_expired(ts: float) -> bool:
    # Persistent cache: never expires unless explicitly refreshed/cleared.
    return False


def _persist_unlocked() -> None:
    """
    Persist the cache to disk when MKVAUTO_PERSIST_DISC_CACHE is enabled (opt-in).
    Caller must hold _cache_lock.
    """
    if not DISK_PERSIST_ENABLED:
        return
    try:
        data = {k: v[1] for k, v in _cache.items()}
        _cache_file.parent.mkdir(parents=True, exist_ok=True)
        _cache_file.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _store_unlocked(key: str, payload: Dict[str, Any]) -> None:
    """Caller must hold _cache_lock."""
    _cache[key] = (time.time(), payload)
    _persist_unlocked()


def _get_any_unlocked(keys: Iterable[str]) -> Optional[Dict[str, Any]]:
    """Caller must hold _cache_lock."""
    for key in keys:
        entry = _cache.get(key)
        if not entry:
            continue
        _, payload = entry
        return payload
    return None


def _get_any(keys: Iterable[str]) -> Optional[Dict[str, Any]]:
    with _cache_lock:
        return _get_any_unlocked(keys)


def _load_persisted() -> None:
    """
    Load cache from disk on startup when persistence is enabled.
    """
    if not DISK_PERSIST_ENABLED:
        return
    if not _cache_file.exists():
        return
    try:
        raw = json.loads(_cache_file.read_text(encoding="utf-8"))
        with _cache_lock:
            for k, v in raw.items():
                _cache[k] = (time.time(), v)
    except Exception:
        pass


def snapshot_entries() -> List[Tuple[str, float, Dict[str, Any]]]:
    """
    Thread-safe shallow copy of cache entries for consistent iteration (e.g. coordinator).
    Returns list of (cache_key, timestamp, payload_copy).
    """
    with _cache_lock:
        return [(key, ts, dict(payload)) for key, (ts, payload) in _cache.items()]

_load_persisted()

async def refresh_all() -> None:
    """
    Enumerate all drives and refresh each disc's metadata if expired or missing.
    """
    async with _LOCK:
        for disc_num, mount in get_drives():
            await _refresh_one(str(disc_num), mount)

async def _refresh_one(disc_num: str, mount_point: str) -> None:
    """
    Refresh a single disc's metadata if the cache is stale or absent.
    Uses Disc Manager instead of Disc.load_db_info().
    """
    existing = _get_any([mount_point, disc_num])
    if existing:
        return  # cache still valid

    try:
        from core.disc_manager import get_disc_info
        info = get_disc_info(str(disc_num), mount_point)
        payload = {
            "disc_num": disc_num,
            "mount_point": mount_point,
            **info,
        }
        set_payload(mount_point, payload)
    except Exception as exc:
        logger.warning("Failed to refresh disc cache for %s (%s): %s", mount_point, disc_num, exc)

def get(key: str) -> Optional[Dict[str, Any]]:
    """
    Retrieve a cached payload by key (disc_num or disc_hash) if present and not expired.
    """
    return _get_any([key])


def get_by_mount_point(mount_point: str) -> Optional[Dict[str, Any]]:
    """
    Return the cached payload for the given device path, or None.

    Since mount_point is now the primary cache key, this is typically a direct
    O(1) lookup.  Falls back to scanning payloads for backward compatibility
    with entries created before the mount_point-keyed migration.
    """
    if not mount_point:
        return None
    # Direct lookup (primary key)
    entry = get(mount_point)
    if entry is not None:
        return entry
    # Fallback: scan all payloads (handles pre-migration entries keyed by disc_num)
    with _cache_lock:
        for _, (_, payload) in _cache.items():
            if payload.get("mount_point") == mount_point:
                return payload
    return None


def get_by_by_id_serial(by_id_serial: str) -> Optional[Dict[str, Any]]:
    """Return the cached payload for the given stable hardware identity (#540).

    Prefer this over :func:`get_by_mount_point` once the caller has resolved
    the by-id serial — it survives the kernel renumbering ``/dev/srN`` that
    motivated the multi-drive identity rewrite.
    """

    if not by_id_serial:
        return None
    entry = get(by_id_serial)
    if entry is not None:
        return entry
    with _cache_lock:
        for _, (_, payload) in _cache.items():
            if payload.get("by_id_serial") == by_id_serial:
                return payload
    return None


def set_payload(primary_key: str, payload: Dict[str, Any]) -> None:
    """
    Immediately store or update the full metadata payload for a disc.

    *primary_key* should be **mount_point** (e.g. ``/dev/sr0``) for multi-drive
    correctness — the device path is stable across MakeMKV drive renumbering.
    For backward compatibility, disc_num is also accepted but callers should
    migrate to mount_point.

    Secondary alias keys (disc_num, disc_hash, disc_id) are created automatically
    from the payload so lookups by any identifier still work.

    If this slot already had an entry (e.g. previous disc), that entry and its
    aliases are removed first so the cache does not retain stale disc_id/disc_hash
    for the same drive after a swap.
    """
    if not payload:
        return

    # Determine mount_point: prefer payload, fall back to primary_key if it looks like a path
    mount_point = (payload.get("mount_point") or "").strip()
    if not mount_point and primary_key.startswith("/dev/"):
        mount_point = primary_key

    # Stable hardware identity (#540). When present, it takes precedence over
    # mount_point for swap detection — a mount_point that's been silently
    # reassigned to a different physical drive by the kernel will have a
    # different by_id_serial, and we must purge ALL state for the old serial.
    new_by_id = (payload.get("by_id_serial") or "").strip()

    with _cache_lock:
        # 1a. Hardware-swap detection: if this mount_point already has a cached
        #     entry whose by_id_serial differs from the incoming one, the kernel
        #     reassigned the device node to a different physical drive. Purge
        #     every entry keyed by the OLD by_id_serial across the cache.
        if mount_point and new_by_id:
            existing = _cache.get(mount_point)
            if existing is not None:
                _, existing_payload = existing
                old_by_id = (existing_payload.get("by_id_serial") or "").strip()
                if old_by_id and old_by_id != new_by_id:
                    swap_stale = [
                        k for k, (_, p) in _cache.items()
                        if (p.get("by_id_serial") or "") == old_by_id
                    ]
                    for k in swap_stale:
                        _cache.pop(k, None)

        # 1b. Clear stale entries for this physical drive (by mount_point) and the primary_key
        keys_to_check = [primary_key]
        if mount_point and mount_point != primary_key:
            keys_to_check.append(mount_point)

        for check_key in keys_to_check:
            if check_key in _cache:
                _, old_payload = _cache[check_key]
                keys_to_remove = [check_key]
                for k in (
                    "mount_point",
                    "disc_num",
                    "disc_hash",
                    "content_hash",
                    "disc_id",
                    "by_id_serial",
                ):
                    v = old_payload.get(k)
                    if v and str(v) not in keys_to_remove:
                        keys_to_remove.append(str(v))
                for k in keys_to_remove:
                    _cache.pop(k, None)

        # Also scan for any other entries with the same mount_point (handles
        # entries created under a different disc_num before renumbering)
        if mount_point:
            stale_keys = []
            for k, (_, p) in _cache.items():
                if p.get("mount_point") == mount_point:
                    stale_keys.append(k)
            for k in stale_keys:
                _cache.pop(k, None)

        _persist_unlocked()

        # 2. Store under mount_point (primary), disc_num, disc_hash, disc_id, by_id_serial
        if mount_point:
            _store_unlocked(mount_point, payload)
        # Also store under primary_key if different from mount_point (backward compat)
        if primary_key != mount_point:
            _store_unlocked(primary_key, payload)
        disc_num = payload.get("disc_num")
        if disc_num and str(disc_num) != mount_point and str(disc_num) != primary_key:
            _store_unlocked(str(disc_num), payload)
        disc_hash = payload.get("disc_hash")
        if disc_hash:
            _store_unlocked(str(disc_hash), payload)
        disc_id = payload.get("disc_id")
        if disc_id:
            _store_unlocked(str(disc_id), payload)
        if new_by_id and new_by_id not in (mount_point, primary_key):
            _store_unlocked(new_by_id, payload)

def set_in_progress(key: str, job_id: str) -> None:
    """
    Mark a disc as having an in-progress job, storing job ID and status.
    *key* should be mount_point (preferred) or disc_num.
    """
    payload = {"status": "running", "job": job_id}
    set_payload(key, payload)

def set(key: str, payload: Dict[str, Any]) -> None:
    """
    Alias to `set_payload`, used by the SSE `events.py` router when receiving updates.
    *key* should be mount_point (preferred) or disc_num.
    """
    set_payload(key, payload)

def clear():
    with _cache_lock:
        _cache.clear()
        _persist_unlocked()


def clear_keys_by_mount_point(mount_point: str) -> bool:
    """
    Remove all cache entries for the given device path (e.g. /dev/sr1).
    Use this on eject so the correct entry is cleared even when disc_num from
    udev (e.g. sr1 -> "1") does not match the app's cache key (e.g. "0").
    Persisted cache is updated so restarts do not re-show the ejected disc.
    Returns True if any entry was cleared.
    """
    if not mount_point:
        return False
    match: Optional[str] = None
    with _cache_lock:
        for k, (_, payload) in _cache.items():
            if payload.get("mount_point") == mount_point:
                match = k
                break
    if match is None:
        return False
    clear_key(match)
    return True


def clear_key(key: str) -> None:
    """
    Remove a single cache entry and all associated aliases (mount_point,
    disc_num, disc_hash, disc_id).
    Only clears cache on ejection/insertion events (called by Drive Manager).
    """
    # CRITICAL: Don't clear cache if makemkvcon is running for this disc (rip in progress)
    # key could be mount_point or disc_num - _find_makemkvcon_process_for_disc handles both
    from core.utils import _find_makemkvcon_process_for_disc
    if key:
        pid, _ = _find_makemkvcon_process_for_disc(key)
        if pid:
            logger.warning(f"Skipping cache clear for key {key}: makemkvcon is still running (PID {pid}, rip in progress)")
            return

    with _cache_lock:
        # Before clearing, get the payload to find all associated keys
        keys_to_clear = {key}
        if key in _cache:
            _, payload = _cache[key]
            for field in ("mount_point", "disc_num", "disc_hash", "content_hash", "disc_id"):
                v = payload.get(field)
                if v:
                    keys_to_clear.add(str(v))

            logger.info(f"Clearing cache for key '{key}' and associated keys: {sorted(keys_to_clear)}")

        # Remove all associated keys
        cleared_count = 0
        for k in keys_to_clear:
            if k in _cache:
                _cache.pop(k, None)
                cleared_count += 1

        if cleared_count > 0:
            _persist_unlocked()
            logger.info(f"Cleared {cleared_count} cache entries for key '{key}'")
