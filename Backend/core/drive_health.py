"""Per-drive health state for fail-closed disc handling (#723 / #724).

When a drive stops responding — the classic symptom is ``mount`` hanging until
``MAKEMKV_MOUNT_TIMEOUT`` expires — the backend used to log the failure and
carry on: the info scan continued, "Info scan completed" was logged, and the
API happily served the *previous* disc's identity for that mount point. A user
could then start a rip that would be filed under the wrong movie.

This module holds the drive-level verdict so the rest of the stack can fail
closed instead:

* :func:`is_drive_fault` classifies a hash/scan exception as drive-level
  (the hardware is not answering) versus disc-level (the medium is fine to
  talk to, we just could not find a Blu-ray/DVD structure on it).
* :func:`mark_drive_unresponsive` records the verdict; :func:`get_drive_health`
  lets the rip gate, the WebSocket initial-state builder and the drives API
  refuse work / surface the message.
* :func:`clear_drive_health` is called whenever the drive demonstrably answers
  again (successful hash, successful eject). On a real unhealthy -> healthy
  transition it also re-arms the fault notifications, so a drive the user fixes
  and then re-breaks alerts again instead of staying inside the dedupe window.

State lives in memory for the lifetime of the API process. It is deliberately
**not** persisted to disk: the only fix for this fault is a USB-level power
cycle, and every path that could clear the flag (insert scan, startup warmup,
eject) re-runs after a restart. A flag that survived the process would instead
risk blocking a drive the user had already fixed.

Keys are always ``mount_point`` (``/dev/sr0``) — the stable physical identity
used everywhere else in the drive layer. MakeMKV ``disc_num`` indices renumber
across hot-plug and must not be used.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("core.drive_health")

# Stable code surfaced to the frontend (409 detail, WebSocket payloads).
# Keep in sync with the frontend's drive-error renderer.
CODE_DRIVE_UNRESPONSIVE = "drive_unresponsive"

# Substrings that identify a drive-level (not disc-level) failure in an
# exception message. ``hash_media_disc`` raises MakeMKVError for the mount
# timeout, but other layers wrap it in plain exceptions, so we match on text
# as well as on type.
_DRIVE_FAULT_MARKERS = (
    "not responding",
    "mount timed out",
    "power cycling",
    "input/output error",
    "no medium found",
)


@dataclass(frozen=True)
class DriveHealth:
    """A recorded drive-level fault for one mount point."""

    mount_point: str
    code: str
    message: str
    detected_at: float

    def to_dict(self) -> dict:
        return {
            "mount_point": self.mount_point,
            "code": self.code,
            "message": self.message,
            "detected_at": self.detected_at,
        }


_lock = threading.Lock()
_unhealthy: dict[str, DriveHealth] = {}

# Level every drive-fault notification is emitted under. Kept here so the emit
# sites and the recovery-invalidation below cannot drift apart.
FAULT_NOTIFICATION_LEVEL = "error_drive_unresponsive"

# Scopes that prefix a fault's ``id_key``. ``None`` is the detection-time alert
# from the scan path; "rip_blocked" is the rip gate refusing to start. Each is a
# distinct dedupe window, so recovery has to clear all of them.
_FAULT_NOTIFICATION_SCOPES: tuple[Optional[str], ...] = (None, "rip_blocked")


def fault_notification_id_key(
    code: str,
    mount_point: str,
    *,
    scope: Optional[str] = None,
) -> str:
    """``id_key`` for a drive-fault notification about *mount_point*.

    Device-scoped rather than event-scoped: a wedged drive alerts once per
    fault, not once per rescan attempt. :func:`clear_drive_health` re-arms it
    when the drive answers again — see ``core.notifications`` for why the
    dedupe window has to be dropped explicitly.
    """
    base = f"{code}:{mount_point}"
    return f"{scope}:{base}" if scope else base


def _rearm_fault_notifications(health: DriveHealth) -> None:
    """Drop the dedupe windows for *health* so a re-fault alerts again.

    Best-effort: recovery must not fail because Redis is unreachable.
    """
    try:
        from core.notifications import clear_notification_dedupe_sync

        for scope in _FAULT_NOTIFICATION_SCOPES:
            clear_notification_dedupe_sync(
                FAULT_NOTIFICATION_LEVEL,
                id_key=fault_notification_id_key(
                    health.code, health.mount_point, scope=scope
                ),
            )
    except Exception as exc:  # pragma: no cover - defensive only
        logger.warning(
            "Failed to re-arm drive-fault notifications for %s: %s",
            health.mount_point, exc,
        )


def is_drive_fault(exc: BaseException | None) -> bool:
    """True when *exc* means "the drive did not answer", not "bad disc".

    A ``FileNotFoundError`` from :func:`core.utils.hash_media_disc` means the
    medium mounted (or was already mounted) but carried no BDMV/VIDEO_TS
    structure — MakeMKV can still often read such a disc through direct disc
    access, so that must NOT be treated as a drive fault or we would refuse
    perfectly rippable discs.
    """
    if exc is None:
        return False
    # Imported lazily: core.utils imports are heavy and this module is pulled
    # in by the rip-start request path.
    try:
        from core.utils import MakeMKVError
    except Exception:  # pragma: no cover - defensive only
        MakeMKVError = ()  # type: ignore[assignment]
    if MakeMKVError and isinstance(exc, MakeMKVError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    text = str(exc).lower()
    return any(marker in text for marker in _DRIVE_FAULT_MARKERS)


def mark_drive_unresponsive(
    mount_point: str,
    message: str,
    code: str = CODE_DRIVE_UNRESPONSIVE,
) -> Optional[DriveHealth]:
    """Record that *mount_point* is not answering. Returns the stored state.

    Re-marking an already-unhealthy drive keeps the original ``detected_at``
    so callers can dedupe notifications on "newly unhealthy".
    """
    mp = (mount_point or "").strip()
    if not mp:
        return None
    text = (message or "").strip() or "Drive is not responding."
    with _lock:
        existing = _unhealthy.get(mp)
        state = DriveHealth(
            mount_point=mp,
            code=code,
            message=text,
            detected_at=existing.detected_at if existing else time.time(),
        )
        _unhealthy[mp] = state
        return state


def get_drive_health(mount_point: str) -> Optional[DriveHealth]:
    """Return the recorded fault for *mount_point*, or None when healthy."""
    mp = (mount_point or "").strip()
    if not mp:
        return None
    with _lock:
        return _unhealthy.get(mp)


def clear_drive_health(mount_point: str) -> bool:
    """Forget any fault for *mount_point*. True when one was cleared.

    Called on every successful hash/eject, so the common case is a no-op. Only
    a real unhealthy -> healthy transition re-arms the fault notifications;
    without that the next genuine fault would stay muted for the rest of the
    dedupe TTL even though the user had fixed and re-broken the drive.
    """
    mp = (mount_point or "").strip()
    if not mp:
        return False
    with _lock:
        previous = _unhealthy.pop(mp, None)
    if previous is None:
        return False
    logger.info("Drive %s is answering again; clearing fault %s", mp, previous.code)
    _rearm_fault_notifications(previous)
    return True


def snapshot() -> list[DriveHealth]:
    """All currently-recorded faults (stable order by mount point)."""
    with _lock:
        return [_unhealthy[k] for k in sorted(_unhealthy)]


def reset_drive_health_for_tests() -> None:
    """Clear all recorded faults (tests only)."""
    with _lock:
        _unhealthy.clear()
