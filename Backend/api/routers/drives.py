"""
Internal drive operations router.
INTERNAL USE ONLY - These endpoints should not be directly accessible from the frontend.
All disc operations should go through the main Backend API -> Disc Manager -> Drive Operations.

This router provides HTTP endpoints for internal use (e.g., for direct testing).
The actual drive operations are in core._drive_operations (underscore prefix = internal only).

NOTE: This router is an allowed caller of _drive_operations, but other routers should
use core.disc_manager instead.
"""
import logging
from fastapi import APIRouter, HTTPException
from core.logging_utils import get_logger

from core._drive_operations import (
    list_drives,
    get_disc_info,
    refresh_disc_info,
    validate_disc_info,
    scan_disc_info,
    hash_disc,
    handle_disc_eject,
    handle_disc_insert,
)

router = APIRouter(prefix="/drives", tags=["drives-internal"])
log = get_logger("api.routers.drives")


@router.get("/healthz")
def healthz():
    """Health check endpoint."""
    log.info("GET /drives/healthz")
    return {"status": "ok"}


@router.get("/drives")
def drives():
    """
    Enumerate drives using MakeMKV.
    INTERNAL USE ONLY - Use /discs endpoint for frontend.
    """
    log.info("GET /drives/drives")
    return list_drives()


@router.get("/usb-topology")
def usb_topology():
    """Live USB bus topology + bandwidth contention warnings (#578).

    Walks ``/sys/bus/usb/devices`` and groups optical drives by bus
    number; flags any sub-SuperSpeed bus carrying 2+ drives as
    bandwidth-contended. The frontend Settings page consumes this to
    surface a remediation hint; the rip-start policy may consult it to
    refuse concurrent rips on a saturated bus.
    """
    from core.usb_topology import snapshot_topology

    return snapshot_topology()


@router.get("/snapshot")
def drives_snapshot():
    """All optical drives the registry can see — including drives with no
    media loaded. Frontend uses this to distinguish "drive present but
    empty" from "drive not connected at all" when deciding whether the
    CTA should read "Insert Disc" or "Drive Not Connected".

    Unlike ``/drives/drives`` (which is filtered to loaded drives so it
    matches the rip-start gate), this returns one row per detected
    ``/dev/srN`` regardless of media-presence.
    """
    from core.drive_registry import snapshot_drives

    return [
        {
            "mount_point": s.mount_point,
            "loaded": s.loaded,
            "volume_label": s.volume_label,
            "media_kind": s.media_kind,
            "by_id_serial": s.identity.by_id_serial,
            "identity_source": s.identity.identity_source,
            "multi_drive_safe": s.identity.multi_drive_safe,
            "vendor": s.identity.vendor,
            "model": s.identity.model,
            "bus": s.identity.bus,
        }
        for s in snapshot_drives()
    ]


@router.get("/discinfo")
def discinfo(disc_num: str, mount_point: str, refresh: bool = False):
    """
    Get disc info (cached or scan).
    INTERNAL USE ONLY - Use /discs/{disc_num}/info endpoint for frontend.
    """
    return get_disc_info(disc_num, mount_point, refresh=refresh)


@router.post("/discinfo/refresh")
def discinfo_refresh(disc_num: str, mount_point: str):
    """
    Force a re-scan of a disc, bypassing any cached payload.
    INTERNAL USE ONLY - Use /discs/{disc_num}/refresh endpoint for frontend.
    """
    return refresh_disc_info(disc_num, mount_point)


@router.post("/discinfo/validate")
def discinfo_validate(disc_num: str, mount_point: str, disc_hash: str):
    """
    Return cached disc info for the drive if the cached hash matches the expected hash.
    INTERNAL USE ONLY.
    """
    return validate_disc_info(disc_num, mount_point, disc_hash)


@router.post("/discinfo/scan")
def discinfo_scan(disc_num: str, mount_point: str):
    """
    Run info scan (with lock check).
    INTERNAL USE ONLY - Called by Disc Manager.
    """
    func_logger = get_logger("api.routers.drives", "discinfo_scan")
    func_logger.debug("Frontend requested disc info scan disc_num=%s mount_point=%s", disc_num, mount_point)
    return scan_disc_info(disc_num, mount_point)


@router.post("/discinfo/hash")
def discinfo_hash(disc_num: str, mount_point: str):
    """
    Calculate hash (with lock check).
    INTERNAL USE ONLY - Called by Disc Manager.
    """
    return hash_disc(disc_num, mount_point)


@router.post("/disc/eject")
def disc_eject(disc_num: str):
    """
    Mark disc as ejected (clears cache).
    INTERNAL USE ONLY - Called by UDS server or Disc Manager.
    """
    return handle_disc_eject(disc_num)


@router.post("/disc/insert")
def disc_insert(disc_num: str, mount_point: str):
    """
    Mark disc as inserted (invalidates cache).
    INTERNAL USE ONLY - Called by UDS server or Disc Manager.
    """
    return handle_disc_insert(disc_num, mount_point)

