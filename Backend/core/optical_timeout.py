"""Raise optical drives' SCSI command timeout from inside the container (#881).

Linux gives each command to an optical drive 30 s
(``/sys/block/srN/device/timeout``), then aborts it and resets the USB device.
A drive working through a slow area can legitimately need longer, and a reset
mid-recovery can wedge it: every later read fails at byte 0 until the drive
is power-cycled (#883). Caught on prod 2026-10-06: USB resets ~31 s apart,
then ``DID_ABORT cmd_age=120s``.

This is applied by the app, never by host configuration — a host udev rule
or sysctl would intrude on setups users already have. ``/sys`` is the host's
sysfs and is writable in the privileged container. The value is per device
and resets on replug or reboot, so it is re-applied at startup, on every
disc event, and on every drive-registry refresh (which also catches a drive
replugged while the app runs).

Only ever raises: a value already at or above the target — e.g. one the user
set higher on the host — is left alone. Where ``/sys`` is read-only
(unprivileged deployments) it logs once per drive and changes nothing.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

ENV_VAR = "MKVAUTO_OPTICAL_COMMAND_TIMEOUT_SECONDS"
DEFAULT_TIMEOUT_SECONDS = 180
SYS_BLOCK = "/sys/block"
_OPTICAL_RE = re.compile(r"^sr\d+$")

# Drives we already warned about (read-only /sys, permission) — warn once each.
_warned: set[str] = set()


def configured_timeout_seconds() -> int:
    """Target timeout; 0 disables the feature."""
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("%s=%r is not a whole number of seconds; using %d",
                       ENV_VAR, raw, DEFAULT_TIMEOUT_SECONDS)
        return DEFAULT_TIMEOUT_SECONDS


def _timeout_path(name: str) -> str:
    return os.path.join(SYS_BLOCK, name, "device", "timeout")


def ensure_drive_timeout(device: str) -> Optional[int]:
    """Raise one drive's command timeout to the target if it is lower.

    ``device`` is ``/dev/sr0`` or ``sr0``. Returns the timeout now in effect,
    or None when it could not be read (no such drive, not an optical drive).
    Never raises.
    """
    name = os.path.basename(device or "")
    if not _OPTICAL_RE.match(name):
        return None
    target = configured_timeout_seconds()
    path = _timeout_path(name)
    try:
        with open(path, encoding="ascii") as fh:
            current = int(fh.read().strip())
    except (OSError, ValueError):
        return None
    if target <= 0 or current >= target:
        return current
    try:
        with open(path, "w", encoding="ascii") as fh:
            fh.write(str(target))
    except OSError as exc:
        if name not in _warned:
            _warned.add(name)
            logger.warning(
                "Could not raise %s's command timeout from %ss to %ss (%s). "
                "Slow reads may reset the drive; run the container privileged "
                "to allow it, or set %s=0 to silence this.",
                name, current, target, exc.strerror or exc, ENV_VAR,
            )
        return current
    logger.info("Raised %s's command timeout from %ss to %ss", name, current, target)
    return target


def ensure_all_drive_timeouts() -> dict[str, Optional[int]]:
    """Apply :func:`ensure_drive_timeout` to every optical drive the kernel
    knows about — the host's view, so drives without a /dev node in the
    container are covered too. Never raises."""
    try:
        names = sorted(n for n in os.listdir(SYS_BLOCK) if _OPTICAL_RE.match(n))
    except OSError:
        return {}
    return {name: ensure_drive_timeout(name) for name in names}
