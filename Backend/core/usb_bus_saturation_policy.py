"""USB-bus-saturation policy gate (#578).

When the user attempts to start a rip on a drive whose USB bus already
hosts an active rip AND the bus is below USB 3.0 SuperSpeed, refuse the
request with a structured 409 so the frontend can surface a confirmation
modal instead of silently letting the bus saturate.

The gate is a thin composition over :mod:`core.usb_topology` and the
``Job`` ORM model — kept in its own module so the policy logic is
testable in isolation and so ``api.routers.jobs`` doesn't need to grow
another inline check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import Session

from api import models as db_models
from core.usb_topology import (
    SUPERSPEED_MBPS_THRESHOLD,
    bus_for_mount_point,
    detect_optical_drives,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SaturationDecision:
    """Result of the bus-saturation check.

    ``allowed`` is False when starting this rip would result in 2+ active
    rips on a sub-SuperSpeed bus. The error payload mirrors the structured
    409 shape the frontend expects: a stable ``code``, the offending
    ``bus`` number + ``speed_mbps``, the list of competing mount_points,
    and a human-readable ``message``.
    """

    allowed: bool
    code: Optional[str] = None
    bus: Optional[int] = None
    speed_mbps: Optional[int] = None
    competing_mount_points: tuple[str, ...] = ()
    message: Optional[str] = None

    def to_409_payload(self) -> dict:
        return {
            "code": self.code,
            "bus": self.bus,
            "speed_mbps": self.speed_mbps,
            "competing_mount_points": list(self.competing_mount_points),
            "message": self.message,
            # The flag the frontend should set on retry — mirrors
            # ``JobCreate.force_concurrent_on_saturated_bus`` so the UX
            # layer doesn't have to hard-code the name.
            "override_field": "force_concurrent_on_saturated_bus",
        }


def _bus_speed(bus: int) -> Optional[int]:
    """Return the link speed of bus ``bus`` from the live topology, or None
    if no optical drive on that bus reports a speed (which can't happen
    for a real connected drive, but the safe path is to skip the gate)."""
    drives = detect_optical_drives()
    speeds = [d.speed_mbps for d in drives if d.bus == bus]
    if not speeds:
        return None
    return max(speeds)


def evaluate_bus_saturation(
    target_mount_point: str,
    db: Session,
    *,
    force_override: bool = False,
) -> SaturationDecision:
    """Decide whether starting a rip on ``target_mount_point`` would
    saturate the USB bus.

    Conditions for refusal:
      1. ``target_mount_point`` resolves to a USB bus (non-USB drives skip).
      2. The bus link speed is below :data:`SUPERSPEED_MBPS_THRESHOLD`.
      3. ≥1 OTHER Job row has ``rip_state='running'`` AND a
         ``mount_point`` resolving to the same bus.
      4. ``force_override`` is False.

    Returns ``allowed=True`` when any condition is unmet — the gate
    intentionally fails open on ambiguous state (sysfs unreadable,
    bus unresolvable) since the user has already been warned at startup
    via the Settings notification.
    """

    if force_override:
        return SaturationDecision(allowed=True)

    bus = bus_for_mount_point(target_mount_point)
    if bus is None:
        return SaturationDecision(allowed=True)

    speed = _bus_speed(bus)
    if speed is None or speed >= SUPERSPEED_MBPS_THRESHOLD:
        return SaturationDecision(allowed=True)

    # Any other actively-ripping job whose mount_point shares this bus?
    competing: list[str] = []
    try:
        active_jobs = (
            db.query(db_models.Job)
            .filter(db_models.Job.rip_state == "running")
            .all()
        )
    except Exception as exc:
        logger.warning("bus saturation: DB query failed (fail-open): %s", exc)
        return SaturationDecision(allowed=True)

    for job in active_jobs:
        other_mp = (job.mount_point or "").strip()
        if not other_mp or other_mp == target_mount_point:
            continue
        other_bus = bus_for_mount_point(other_mp)
        if other_bus == bus:
            competing.append(other_mp)

    if not competing:
        return SaturationDecision(allowed=True)

    msg = (
        f"USB Bus {bus} ({speed} Mbps) already hosts an active rip on "
        f"{', '.join(competing)}. Concurrent rips on this bus will "
        f"saturate it and trigger USB controller resets (see #578). "
        f"Move one drive to a USB 3.0 (SuperSpeed) port, or acknowledge "
        f"the risk to override."
    )
    return SaturationDecision(
        allowed=False,
        code="usb_bus_saturation_risk",
        bus=bus,
        speed_mbps=speed,
        competing_mount_points=tuple(competing),
        message=msg,
    )
