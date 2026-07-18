"""
In-memory state tracking for active drive operations.
Tracks which drives are currently running scans or rips.

Keyed by **mount_point** (device path, e.g. ``/dev/sr0``), the stable physical
identity of a drive.  MakeMKV ``disc_num`` indices are volatile across hot-plug
events and must not be used as primary keys.
"""
import time
from typing import Dict, Optional
from dataclasses import dataclass
from threading import Lock


@dataclass
class ActiveOperation:
    disc_num: str
    mount_point: str
    operation_type: str  # "scan", "rip", "hash"
    job_id: Optional[str] = None
    started_at: float = 0.0


class DriveState:
    """Thread-safe in-memory state tracker for drive operations.

    All lookups are by **mount_point** (device path).  ``disc_num`` is stored
    on the :class:`ActiveOperation` for display/logging but is not part of the key.
    """
    def __init__(self):
        self._lock = Lock()
        self._active_operations: Dict[str, ActiveOperation] = {}  # key: mount_point

    def is_drive_busy(self, disc_num: str, mount_point: str) -> bool:
        """Check if a drive is currently busy with any operation."""
        with self._lock:
            return mount_point in self._active_operations

    def get_operation(self, disc_num: str, mount_point: str) -> Optional[ActiveOperation]:
        """Get the active operation for a drive, if any."""
        with self._lock:
            return self._active_operations.get(mount_point)

    def start_operation(self, disc_num: str, mount_point: str, operation_type: str, job_id: Optional[str] = None):
        """Mark a drive as busy with an operation."""
        with self._lock:
            self._active_operations[mount_point] = ActiveOperation(
                disc_num=disc_num,
                mount_point=mount_point,
                operation_type=operation_type,
                job_id=job_id,
                started_at=time.time()
            )

    def end_operation(self, disc_num: str, mount_point: str):
        """Mark a drive operation as complete."""
        with self._lock:
            self._active_operations.pop(mount_point, None)

    def get_active_operations(self) -> Dict[str, ActiveOperation]:
        """Get all active operations (for debugging/monitoring)."""
        with self._lock:
            return self._active_operations.copy()

    def get_operation_by_job_id(self, job_id: str) -> Optional[ActiveOperation]:
        """Get active operation by job_id, if any."""
        with self._lock:
            for op in self._active_operations.values():
                if op.job_id == job_id:
                    return op
            return None


# Global singleton instance
_drive_state = DriveState()


def get_drive_state() -> DriveState:
    """Get the global drive state instance."""
    return _drive_state

