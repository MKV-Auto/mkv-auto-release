"""Stable drive identity resolution across all optical-drive bus types.

Resolves ``/dev/srN`` mount points to stable hardware identifiers by walking
``/dev/disk/by-id/`` symlinks. Supports USB, SATA/ATA, SCSI, and NVMe drives.
Falls back to ``/dev/disk/by-path/`` and then ``/sys/block/srN/device/``.

A drive is considered *multi-drive safe* only when its identity resolves via
``/dev/disk/by-id/`` (any of the ``wwn-``/``wwid-``/``ata-``/``usb-``/``scsi-``/
``nvme-`` prefixes). Drives that fall back to ``by-path`` or sysfs are blocked
from concurrent multi-drive operations by the gatekeeper, because their
identity is not stable across USB bus resets / kernel renumbering that have
been observed on slim bus-powered drives like the Pioneer BD-RW BDR-XD06U.

See ``docs/DRIVES_DISC_CACHE_AND_MULTI_DRIVE.md`` for the architectural
rationale and GitHub issue #540 for the diagnostic that motivated this.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal, Optional

logger = logging.getLogger(__name__)


# Ordered by stability — first match wins. ``wwn``/``wwid`` are SCSI-level
# World Wide Names that persist across firmware updates; ``ata``/``usb``/
# ``scsi``/``nvme`` carry the bus-specific stable serial.
BY_ID_PRECEDENCE = ("wwn-", "wwid-", "ata-", "usb-", "scsi-", "nvme-")

BY_ID_DIR = "/dev/disk/by-id"
BY_PATH_DIR = "/dev/disk/by-path"
SYS_BLOCK_DIR = "/sys/block"
DEV_DIR = "/dev"


IdentitySource = Literal["by-id", "by-path", "sysfs", "unknown"]


@dataclass(frozen=True)
class DriveIdentity:
    """Stable hardware identity for an optical drive.

    ``by_id_serial`` holds the SERIAL portion of the by-id symlink name when
    ``identity_source == "by-id"``. For fallback sources it is a synthetic
    identifier derived from the available signals (PCI path for by-path,
    vendor+model+SCSI tuple for sysfs, mount_point for unknown).

    ``multi_drive_safe`` is True iff identity resolved via ``/dev/disk/by-id/``.
    The gatekeeper uses this to decide whether the drive may participate in
    multi-drive concurrent operations.
    """

    by_id_serial: str
    vendor: str
    model: str
    bus: str
    by_id_name: str
    hardware_name: Optional[str]
    identity_source: IdentitySource

    @property
    def multi_drive_safe(self) -> bool:
        return self.identity_source == "by-id"


def resolve_drive_identity(
    mount_point: str,
    *,
    by_id_dir: str = BY_ID_DIR,
    by_path_dir: str = BY_PATH_DIR,
    sys_block_dir: str = SYS_BLOCK_DIR,
    hardware_name: Optional[str] = None,
) -> DriveIdentity:
    """Resolve a stable identity for the drive at ``mount_point``.

    Walks ``by_id_dir`` for a symlink whose target is ``mount_point``,
    preferring the most stable prefix in ``BY_ID_PRECEDENCE``. Falls back
    through ``by_path_dir`` and ``sys_block_dir``. Always returns a
    DriveIdentity — when nothing resolves, ``identity_source == "unknown"``
    and the gatekeeper will refuse operations.

    ``hardware_name`` is the optional MakeMKV-side hardware string (parsed
    from the DRV line) — stored verbatim for cross-checking and debug logs.
    """

    target = os.path.realpath(mount_point)

    identity = _try_by_id(target, by_id_dir, hardware_name)
    if identity is not None:
        return identity

    identity = _try_by_path(target, by_path_dir, mount_point, hardware_name)
    if identity is not None:
        return identity

    identity = _try_sysfs(mount_point, sys_block_dir, hardware_name)
    if identity is not None:
        return identity

    logger.warning(
        "drive_identity: no stable identifier found for %s; multi-drive operations will be blocked",
        mount_point,
    )
    return DriveIdentity(
        by_id_serial=f"unknown:{os.path.basename(mount_point)}",
        vendor="",
        model="",
        bus="unknown",
        by_id_name="",
        hardware_name=hardware_name,
        identity_source="unknown",
    )


def resolve_current_mount_point_for_serial(
    by_id_serial: str,
    *,
    by_id_dir: str = BY_ID_DIR,
    by_path_dir: str = BY_PATH_DIR,
    sys_block_dir: str = SYS_BLOCK_DIR,
    dev_dir: str = DEV_DIR,
) -> Optional[str]:
    """Return the current ``/dev/srN`` that resolves to ``by_id_serial``, if any.

    Reverse of :func:`resolve_drive_identity`. Use this at operation-execution
    time to defend against ``/dev/srN`` renumbering — if the cached mount_point
    no longer matches the persisted ``by_id_serial``, the operation should
    refresh to whichever ``/dev/srN`` *currently* resolves to the same hardware,
    or fail loudly if the drive is no longer attached at all.
    """

    if not by_id_serial:
        return None
    identity_map = build_identity_map(
        by_id_dir=by_id_dir,
        by_path_dir=by_path_dir,
        sys_block_dir=sys_block_dir,
        dev_dir=dev_dir,
    )
    for mount_point, identity in identity_map.items():
        if identity.by_id_serial == by_id_serial:
            return mount_point
    return None


def build_identity_map(
    *,
    by_id_dir: str = BY_ID_DIR,
    by_path_dir: str = BY_PATH_DIR,
    sys_block_dir: str = SYS_BLOCK_DIR,
    dev_dir: str = DEV_DIR,
) -> dict[str, DriveIdentity]:
    """Return ``{mount_point: DriveIdentity}`` for every optical drive on the host.

    Scans ``sys_block_dir`` for ``sr*`` block devices and resolves each through
    :func:`resolve_drive_identity`. Drives without a corresponding ``dev_dir/sr*``
    node are skipped — that asymmetric state (sysfs present, ``/dev`` node
    missing) was observed in the 2026-06 diagnostic and represents a drive the
    container can read metadata about but cannot actually open.
    """

    result: dict[str, DriveIdentity] = {}
    if not os.path.isdir(sys_block_dir):
        return result

    for name in os.listdir(sys_block_dir):
        if not name.startswith("sr"):
            continue
        mount_point = os.path.join(dev_dir, name)
        if not os.path.exists(mount_point):
            continue
        result[mount_point] = resolve_drive_identity(
            mount_point,
            by_id_dir=by_id_dir,
            by_path_dir=by_path_dir,
            sys_block_dir=sys_block_dir,
        )
    return result


# --- private helpers -------------------------------------------------------


def _try_by_id(
    target: str,
    by_id_dir: str,
    hardware_name: Optional[str],
) -> Optional[DriveIdentity]:
    if not os.path.isdir(by_id_dir):
        return None

    # Collect every symlink in by_id_dir whose realpath matches target.
    # Each entry maps to a precedence rank; we pick the most stable.
    candidates: list[tuple[int, str]] = []
    for entry in os.scandir(by_id_dir):
        try:
            if os.path.realpath(entry.path) != target:
                continue
        except OSError:
            continue
        for rank, prefix in enumerate(BY_ID_PRECEDENCE):
            if entry.name.startswith(prefix):
                candidates.append((rank, entry.name))
                break

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0])
    rank, name = candidates[0]
    prefix = BY_ID_PRECEDENCE[rank]
    bus = prefix.rstrip("-")
    vendor, model, serial = _parse_by_id_name(name, prefix)

    return DriveIdentity(
        by_id_serial=serial,
        vendor=vendor,
        model=model,
        bus=bus,
        by_id_name=name,
        hardware_name=hardware_name,
        identity_source="by-id",
    )


def _try_by_path(
    target: str,
    by_path_dir: str,
    mount_point: str,
    hardware_name: Optional[str],
) -> Optional[DriveIdentity]:
    if not os.path.isdir(by_path_dir):
        return None

    for entry in os.scandir(by_path_dir):
        try:
            if os.path.realpath(entry.path) != target:
                continue
        except OSError:
            continue
        logger.warning(
            "drive_identity: %s resolved only via by-path (%s); multi-drive blocked",
            mount_point,
            entry.name,
        )
        return DriveIdentity(
            by_id_serial=f"by-path:{entry.name}",
            vendor="",
            model="",
            bus="by-path",
            by_id_name=entry.name,
            hardware_name=hardware_name,
            identity_source="by-path",
        )
    return None


def _try_sysfs(
    mount_point: str,
    sys_block_dir: str,
    hardware_name: Optional[str],
) -> Optional[DriveIdentity]:
    name = os.path.basename(mount_point)
    device_dir = os.path.join(sys_block_dir, name, "device")
    if not os.path.isdir(device_dir):
        return None

    vendor = _read_sysfs(device_dir, "vendor")
    model = _read_sysfs(device_dir, "model")
    if not vendor and not model:
        return None

    # Synthetic serial: vendor+model+device-name. NOT stable across
    # kernel renumbering — caller must treat this as multi-drive-unsafe.
    synthetic = f"sysfs:{vendor}:{model}:{name}".replace(" ", "_")
    logger.warning(
        "drive_identity: %s resolved only via sysfs (%s/%s); multi-drive blocked",
        mount_point,
        vendor,
        model,
    )
    return DriveIdentity(
        by_id_serial=synthetic,
        vendor=vendor,
        model=model,
        bus="sysfs",
        by_id_name="",
        hardware_name=hardware_name,
        identity_source="sysfs",
    )


def _parse_by_id_name(name: str, prefix: str) -> tuple[str, str, str]:
    """Split a by-id symlink name into ``(vendor, model, serial)``.

    Common formats handled:

    - ``usb-PIONEER_BD-RW_BDR-XD06U_1958040110900395-0:0`` (USB)
    - ``ata-QEMU_DVD-ROM_QM00003`` (SATA/ATA)
    - ``scsi-0QEMU_QEMU_HARDDISK_drive-scsi0`` (SCSI)
    - ``wwn-0x5000c500abcdef01`` (WWN — vendor/model unknown from name)
    """

    body = name[len(prefix):] if name.startswith(prefix) else name

    # USB symlinks carry a trailing interface designator like ``-0:0``.
    last_dash = body.rfind("-")
    if last_dash != -1 and ":" in body[last_dash:]:
        body = body[:last_dash]

    parts = body.split("_")
    if not parts:
        return ("", "", body)
    if len(parts) == 1:
        # wwn-* / wwid-* — single opaque token.
        return ("", "", parts[0])
    if len(parts) == 2:
        return ("", parts[0], parts[1])
    vendor = parts[0]
    serial = parts[-1]
    model = " ".join(parts[1:-1])
    return (vendor, model, serial)


def _read_sysfs(device_dir: str, attr: str) -> str:
    try:
        with open(os.path.join(device_dir, attr), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""
