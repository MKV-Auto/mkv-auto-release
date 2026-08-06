"""Tell "no disc" apart from "a disc the drive cannot engage".

A disc inserted upside down, misseated, or too damaged to spin up produces a
state the app previously reported as an ordinary eject: the user inserts
something, nothing happens, and no message explains why. Diagnosed on a real
drive after exactly that report.

The state is distinguishable. The drive *senses* media — ``CDROM_DRIVE_STATUS``
returns ``CDS_DISC_OK`` or ``CDS_DRIVE_NOT_READY`` — while every path that
would read it fails with ``ENOMEDIUM``:

    dd if=/dev/sr0     -> "No medium found"
    sg_get_config      -> "No current profile"
    sg_turs            -> "Not Ready / Incompatible medium installed"
    makemkvcon         -> drive listed, disc name empty, no titles
    CDROM_DRIVE_STATUS -> 4 (disc OK)          <- the drive senses something

An empty tray looks different: ``CDS_NO_DISC`` or ``CDS_TRAY_OPEN``. That gap
is the whole signal, and it is what lets us say "reseat the disc" instead of
silently treating the insert as an eject.
"""
from __future__ import annotations

import fcntl
import logging
import os

logger = logging.getLogger(__name__)

# Linux cdrom API (uapi/linux/cdrom.h).
CDROM_DRIVE_STATUS = 0x5326
CDS_NO_DISC = 1
CDS_TRAY_OPEN = 2
CDS_DRIVE_NOT_READY = 3
CDS_DISC_OK = 4

# States in which the drive believes something is in the tray.
_SENSES_MEDIA = (CDS_DISC_OK, CDS_DRIVE_NOT_READY)


def _drive_status(device: str) -> int | None:
    """Raw CDROM_DRIVE_STATUS, or None if the drive cannot answer."""
    try:
        fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
        try:
            return fcntl.ioctl(fd, CDROM_DRIVE_STATUS)
        finally:
            os.close(fd)
    except Exception as exc:  # noqa: BLE001 - diagnosis must never raise
        logger.debug("media_diagnostics: drive status unavailable for %s: %s", device, exc)
        return None


def _medium_is_readable(device: str) -> bool:
    """True when a single sector can actually be read.

    Opening without ``O_NONBLOCK`` is what surfaces ``ENOMEDIUM``: the
    non-blocking open deliberately succeeds on an empty optical drive, which
    is why a plain open is not evidence of media.
    """
    try:
        fd = os.open(device, os.O_RDONLY)
    except Exception:
        return False
    try:
        return len(os.read(fd, 2048)) > 0
    except Exception:
        return False
    finally:
        os.close(fd)


def medium_present_but_unreadable(device: str) -> bool:
    """True when the drive senses media it cannot engage.

    Deliberately conservative: it must be able to read the drive status *and*
    see a media-sensing state *and* fail to read a sector. Anything else —
    including a drive that cannot report status — returns False, because a
    false "reseat the disc" alert on a genuinely empty drive would train the
    user to ignore the message.
    """
    status = _drive_status(device)
    if status is None or status not in _SENSES_MEDIA:
        return False
    return not _medium_is_readable(device)


def notify_unreadable_medium(device: str) -> None:
    """Tell the user their disc needs reseating. Never raises.

    Deduped per device by ``id_key``, so a disc left misseated does not
    produce an alert on every udev event — the same discipline as the other
    drive alerts (#723, #724).
    """
    try:
        from core.notifications import emit_notification_sync

        emit_notification_sync(
            message=(
                f"A disc is in the drive at {device}, but it cannot be read. "
                "This usually means the disc is upside down or not seated "
                "properly — eject it and reinsert it. If it still fails, the "
                "disc may be dirty, damaged, or a type this drive cannot read."
            ),
            kind="warning",
            level="action_required",
            title="Disc unreadable — try reseating it",
            id_key=f"unreadable_medium:{device}",
        )
    except Exception as exc:  # noqa: BLE001 - a failed alert must not break scanning
        logger.warning("unreadable-medium notification dispatch failed: %s", exc)
