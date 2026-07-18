"""
Per-drive-slot state for classifying udev optical events.

We cannot rely on udev "change" alone to mean a physical reinsert: the kernel often
emits extra changes after SCSI unit attention / media settle. This module tracks:

- unknown: no completed scan since process start (treat like strong insert).
- stable: last cycle finished a successful insert scan without an eject; a udev
  "change" that only sees media still present may be weak noise.
- absent: eject was processed; the next presence is a strong insert.

All keys are **mount_point** (device path, e.g. ``/dev/sr0``), which is the
stable physical identity of a drive.  MakeMKV ``disc_num`` indices are volatile
across hot-plug events and must not be used as slot-state keys.

Insert scans are single-flight per mount_point so overlapping udev bursts do
not start concurrent hash/MakeMKV work on the same device.
"""
from __future__ import annotations

import threading
from typing import Literal

SlotState = Literal["unknown", "stable", "absent"]

_lock = threading.Lock()
# Keyed by mount_point (e.g. "/dev/sr0")
_slot_state: dict[str, SlotState] = {}
_insert_scan_mounts: set[str] = set()


def get_slot_state(mount_point: str) -> SlotState:
    """Return the slot state for the given device path."""
    return _slot_state.get(str(mount_point), "unknown")


def mark_slot_absent(mount_point: str) -> None:
    """Call when media left the drive (eject path).  *mount_point* is the device path."""
    with _lock:
        _slot_state[str(mount_point)] = "absent"


def mark_slot_stable(mount_point: str) -> None:
    """Call after a successful proactive insert scan.  *mount_point* is the device path."""
    with _lock:
        _slot_state[str(mount_point)] = "stable"


def should_treat_change_as_weak_insert(mount_point: str) -> bool:
    """
    True if a udev *change* remapped to insert may be SCSI/media noise:
    we already completed a scan for this slot and have not seen eject since.
    """
    return get_slot_state(str(mount_point)) == "stable"


def get_scanning_mount_points() -> frozenset[str]:
    """Return a snapshot of mount_points currently undergoing insert scans.

    Used by the initial-state endpoint so the frontend can show "scanning"
    cards for drives whose scan is in progress but not yet in disc_cache.
    """
    with _lock:
        return frozenset(_insert_scan_mounts)


def try_begin_insert_scan(mount_point: str) -> bool:
    """Return True if this mount may start an insert scan; False if one is already running."""
    with _lock:
        if mount_point in _insert_scan_mounts:
            return False
        _insert_scan_mounts.add(mount_point)
        return True


def end_insert_scan(mount_point: str) -> None:
    with _lock:
        _insert_scan_mounts.discard(mount_point)


def reset_disc_slot_state_for_tests() -> None:
    """Clear all in-memory slot and scan state (tests only)."""
    with _lock:
        _slot_state.clear()
        _insert_scan_mounts.clear()
