"""USB bus topology + bandwidth contention detection (#578).

When multiple optical drives share a single sub-SuperSpeed USB host
controller, sustained concurrent reads (especially UHD Blu-ray rips)
saturate the 480 Mbps bus, trigger controller resets, and cascade into
device disconnects affecting every drive on that bus.

The 2026-06-21 live two-rip test (#562) reproduced this cleanly: both
drives on Bus 02 (USB 2.0, 480 Mbps), Fallout rip caused the Pioneer's
USB stack to reset at ~T+50min, the reset propagated across the bus,
both drives momentarily disconnected, the Pioneer came back broken
and lost 66/80 titles to ``MSG:2003 Posix error - No such device``.

This module walks ``/sys/bus/usb/devices/*`` and groups optical drives
by ``busnum``, emitting a warning for any bus that:

  - hosts >= 2 optical drives, AND
  - reports a link speed below 5000 Mbps (i.e., below USB 3.0 SuperSpeed)

The 5000 Mbps threshold is deliberately strict: USB 3.0 has enough
headroom (5 Gbps = 625 MB/s) that two BD rips don't compete; anything
slower (USB 2.0 high-speed at 480 Mbps, USB 1.1 at 12 Mbps) is at risk.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import asdict, dataclass
from typing import Optional

logger = logging.getLogger(__name__)


SYS_BUS_USB_DEVICES = "/sys/bus/usb/devices"

# USB 3.0 SuperSpeed = 5000 Mbps. Drives that resolve below this on a shared
# bus are flagged. SuperSpeed Plus (USB 3.1+, 10000+ Mbps) safely covers any
# realistic optical-drive bandwidth budget.
SUPERSPEED_MBPS_THRESHOLD = 5000

# Regex matched against the device's "product" sysfs attribute. Pioneer
# BD-XD06U/07U report "Pioneer Blu-ray Drive"; ASUS BW-16D1HT enclosures
# report "External Drive"; LG/Sony optical drives typically include
# "BD", "DVD", "CD-ROM", or "Optical" in the product string. ``\bbd\b``
# catches "LG BD Writer" while ``bd-`` catches "BD-RE" etc.
OPTICAL_PRODUCT_RE = re.compile(
    r"blu|optical|\bbd\b|bd-|dvd|cd-?rom|external\s*drive",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OpticalDrive:
    """One optical drive observed via sysfs."""

    bus: int
    speed_mbps: int
    product: str
    manufacturer: str
    serial: str
    sysfs_path: str


@dataclass(frozen=True)
class BusContentionWarning:
    """Two or more optical drives on a single sub-SuperSpeed bus."""

    bus: int
    speed_mbps: int
    drive_count: int
    drives: list[str]
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _read_attr(path: str, attr: str) -> Optional[str]:
    try:
        with open(os.path.join(path, attr), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def detect_optical_drives(
    *, sys_bus_usb_devices: str | None = None,
) -> list[OpticalDrive]:
    """Walk sysfs and return one ``OpticalDrive`` per detected optical drive.

    Filters by ``product`` sysfs string matching ``OPTICAL_PRODUCT_RE``.
    Devices missing ``speed`` or ``busnum`` (root hubs, interface dirs)
    are silently skipped — the iteration is best-effort. Test envs without
    a real sysfs return an empty list rather than failing.

    The ``sys_bus_usb_devices`` default resolves to the module-level
    :data:`SYS_BUS_USB_DEVICES` at call time, so test monkeypatching the
    constant takes effect (default-arg values bind at function definition).
    """

    if sys_bus_usb_devices is None:
        sys_bus_usb_devices = SYS_BUS_USB_DEVICES

    drives: list[OpticalDrive] = []
    if not os.path.isdir(sys_bus_usb_devices):
        return drives

    for dev_dir in glob.glob(os.path.join(sys_bus_usb_devices, "*")):
        product = _read_attr(dev_dir, "product")
        if not product or not OPTICAL_PRODUCT_RE.search(product):
            continue
        speed_str = _read_attr(dev_dir, "speed")
        bus_str = _read_attr(dev_dir, "busnum")
        if not speed_str or not bus_str:
            continue
        try:
            speed = int(speed_str)
            bus = int(bus_str)
        except ValueError:
            continue
        drives.append(
            OpticalDrive(
                bus=bus,
                speed_mbps=speed,
                product=product,
                manufacturer=_read_attr(dev_dir, "manufacturer") or "",
                serial=_read_attr(dev_dir, "serial") or "",
                sysfs_path=dev_dir,
            )
        )
    return drives


def detect_contention_warnings(
    drives: list[OpticalDrive],
) -> list[BusContentionWarning]:
    """Group ``drives`` by ``bus`` and emit a warning per saturated bus.

    A bus is saturated when:
      - it carries 2+ optical drives, AND
      - its link speed is below :data:`SUPERSPEED_MBPS_THRESHOLD`.
    """

    by_bus: dict[int, list[OpticalDrive]] = {}
    for d in drives:
        by_bus.setdefault(d.bus, []).append(d)

    warnings: list[BusContentionWarning] = []
    for bus, devs in by_bus.items():
        if len(devs) < 2:
            continue
        bus_speed = max(d.speed_mbps for d in devs)
        if bus_speed >= SUPERSPEED_MBPS_THRESHOLD:
            continue
        names = [d.product for d in devs]
        warnings.append(
            BusContentionWarning(
                bus=bus,
                speed_mbps=bus_speed,
                drive_count=len(devs),
                drives=names,
                message=(
                    f"USB Bus {bus} ({bus_speed} Mbps) hosts {len(devs)} optical "
                    f"drives. Concurrent rips will saturate this bus and trigger "
                    f"USB resets — see #578. Move at least one drive to a USB 3.0 "
                    f"(SuperSpeed) port to isolate."
                ),
            )
        )
    return warnings


def bus_for_mount_point(
    mount_point: str,
    *,
    sys_block_dir: str = "/sys/block",
) -> Optional[int]:
    """Return the USB bus number that hosts the optical drive at ``mount_point``,
    or ``None`` when the drive isn't a USB device (e.g. ATAPI/SATA optical drives
    or a missing device node).

    Resolves ``/sys/block/srN/device`` to its real path, then walks up the
    parent chain looking for a directory that has a ``busnum`` attribute —
    that's the USB device-level dir. Example real path:

        /sys/devices/pci.../usb2/2-3/2-3:1.0/host4/target4:0:0/4:0:0:0

    Walking up from the device dir we hit ``2-3`` which has ``busnum=2``.
    """
    if not mount_point:
        return None
    name = os.path.basename(mount_point.rstrip("/"))
    if not name.startswith("sr"):
        return None
    device_link = os.path.join(sys_block_dir, name, "device")
    try:
        cursor = os.path.realpath(device_link)
    except OSError:
        return None
    if not cursor or not os.path.isdir(cursor):
        return None
    # Walk up. Limit iterations defensively — sysfs depth is bounded.
    for _ in range(20):
        if cursor in ("/", ""):
            break
        bus_str = _read_attr(cursor, "busnum")
        if bus_str:
            try:
                return int(bus_str)
            except ValueError:
                return None
        parent = os.path.dirname(cursor)
        if parent == cursor:
            break
        cursor = parent
    return None


def snapshot_topology() -> dict:
    """Convenience: return the full topology + warnings as a serializable dict."""
    drives = detect_optical_drives()
    warnings = detect_contention_warnings(drives)
    return {
        "drives": [asdict(d) for d in drives],
        "warnings": [w.to_dict() for w in warnings],
    }
