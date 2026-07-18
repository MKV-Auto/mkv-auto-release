"""Detect hardware-identity swaps at the same ``/dev/srN`` between snapshots.

The 2026-06 diagnostic showed that the kernel can silently reassign
``/dev/sr1`` from one physical drive to another after a USB bus reset
(Pioneer XD06U triggers this on tray-eject; the cascade affects every
USB drive on the same controller). When that happens, any rip in flight
on the old drive at that mount_point would route subsequent makemkvcon
commands to the WRONG hardware.

This module is the post-#549 layer that catches the runtime case: on
every udev event the backend rebuilds the identity map (cheap, by-id
symlink walk) and compares it to the cached previous map. Any mount_point
whose ``by_id_serial`` changed is reported as a :class:`DriveSwap`, which
the caller then uses to fail the affected rip jobs.

This intentionally treats *disconnects* and *first appearances* as NOT
swaps — they don't share the cross-contamination risk that an
identity change at a still-occupied mount_point does.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from core.drive_identity import DriveIdentity


@dataclass(frozen=True)
class DriveSwap:
    """A single same-mount_point identity change.

    ``previous_serial`` is the ``by_id_serial`` from the cached map;
    ``current_serial`` is the value just resolved. The caller fails any
    active rip job whose ``Job.drive_by_id_serial`` matches the previous
    serial — that job was created when the mount_point pointed at a
    different physical drive and would risk corruption if continued.
    """

    mount_point: str
    previous_serial: str
    current_serial: str


def detect_drive_swaps(
    previous: Mapping[str, DriveIdentity],
    current: Mapping[str, DriveIdentity],
) -> list[DriveSwap]:
    """Return the list of mount_points whose ``by_id_serial`` changed.

    A drive that disappears entirely (``mount_point`` in ``previous`` but
    not ``current``) is **not** reported — that is handled by the existing
    drive-eject path. A drive that appears for the first time
    (``mount_point`` in ``current`` only) is **not** reported — that is a
    fresh registration, not a swap.

    Only mount_points present in **both** maps with a different
    ``by_id_serial`` are returned.
    """

    swaps: list[DriveSwap] = []
    for mount_point, prev_id in previous.items():
        cur_id = current.get(mount_point)
        if cur_id is None:
            continue
        prev_serial = (prev_id.by_id_serial or "").strip()
        cur_serial = (cur_id.by_id_serial or "").strip()
        if not prev_serial or not cur_serial:
            # If either side has no serial, we can't make a confident
            # swap claim — bail.
            continue
        if prev_serial != cur_serial:
            swaps.append(
                DriveSwap(
                    mount_point=mount_point,
                    previous_serial=prev_serial,
                    current_serial=cur_serial,
                )
            )
    return swaps
