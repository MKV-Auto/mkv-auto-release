"""Dolby Vision 7 -> 8.1 conversion: settings, library catalog and queue (#894).

    GET  /system/dv7/config                 settings + the library (transfer destination) + last scan
    PUT  /system/dv7/config                 update the switches
    POST /system/dv7/scan                   catalog the library now
    GET  /system/dv7/library                counts by status
    GET  /system/dv7/library/files          catalog rows (filter by status list / DV profile)
    POST /system/dv7/library/files/{id}/retry  re-queue a failed/skipped file

Everything is disabled by default. Scanning and converting only happen while
the master switch is on. The library is the active transfer destination
(#902, ``core.library_destination``); there is no separate folder setting.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import models as db_models
from api.database import get_db
from core import library_catalog, library_destination
from core import settings

router = APIRouter(prefix="/system/dv7", tags=["dv7"])


class Dv7Destination(BaseModel):
    available: bool
    mode: Optional[str] = None
    name: Optional[str] = None
    display: Optional[str] = None
    problem: Optional[str] = None


class Dv7LastScan(BaseModel):
    at: str
    ok: Optional[bool] = None      # None while the scan is still running
    destination: Optional[str] = None
    files: Optional[int] = None
    dv7: Optional[int] = None
    queued: Optional[int] = None
    unreadable: Optional[int] = None
    problem: Optional[str] = None
    heartbeat: Optional[str] = None


class Dv7Config(BaseModel):
    enabled: bool
    convert_new_rips: bool
    destination: Dv7Destination
    last_scan: Optional[Dv7LastScan] = None


class Dv7ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    convert_new_rips: Optional[bool] = None


class Dv7LibraryFile(BaseModel):
    id: str
    path: str
    library_root: str
    size: Optional[int] = None
    video_codec: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    dv_profile: Optional[int] = None
    duration: Optional[float] = None
    status: str
    progress: Optional[float] = None
    error: Optional[str] = None
    scanned_at: Optional[str] = None
    converted_at: Optional[str] = None


def _config_out(db: Session) -> Dv7Config:
    cfg = settings.get_dv7_conversion()
    dest = library_destination.describe(db)
    return Dv7Config(
        enabled=cfg["enabled"],
        convert_new_rips=cfg["convert_new_rips"],
        destination=Dv7Destination(
            available=dest.available, mode=dest.mode, name=dest.name,
            display=dest.display, problem=dest.problem,
        ),
        last_scan=cfg["last_scan"],
    )


def _dispatch_scan() -> None:
    from workers.tasks import library_scan
    library_scan.apply_async()


@router.get("/config", response_model=Dv7Config)
def get_dv7_config(db: Session = Depends(get_db)) -> Dv7Config:
    return _config_out(db)


@router.put("/config", response_model=Dv7Config)
def update_dv7_config(payload: Dv7ConfigUpdate, db: Session = Depends(get_db)) -> Dv7Config:
    """Save the switches. Turning the feature on starts a scan of the
    transfer destination when there is one the scan can reach."""
    before = settings.get_dv7_conversion()
    after = settings.set_dv7_conversion(
        enabled=payload.enabled,
        convert_new_rips=payload.convert_new_rips,
    )
    if after["enabled"] and not before["enabled"] and library_destination.describe(db).available:
        _dispatch_scan()
    return _config_out(db)


@router.post("/scan")
def scan_now(db: Session = Depends(get_db)) -> dict:
    if not settings.get_dv7_conversion()["enabled"]:
        raise HTTPException(409, detail="Dolby Vision conversion is turned off.")
    dest = library_destination.describe(db)
    if not dest.available:
        raise HTTPException(409, detail=dest.problem)
    _dispatch_scan()
    return {"queued": True}


@router.get("/library")
def library_summary(db: Session = Depends(get_db)) -> dict:
    return library_catalog.catalog_summary(db)


@router.get("/library/files", response_model=List[Dv7LibraryFile])
def library_files(
    status: Optional[str] = None,
    dv7_only: bool = True,
    dv_profile: Optional[int] = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[Dv7LibraryFile]:
    """``status`` takes one status or a comma-separated list (the Settings
    page's default view is ``queued,converting``)."""
    q = db.query(db_models.LibraryMediaFile)
    statuses = [s.strip() for s in (status or "").split(",") if s.strip()]
    if statuses:
        q = q.filter(db_models.LibraryMediaFile.status.in_(statuses))
    if dv_profile is not None:
        q = q.filter(db_models.LibraryMediaFile.dv_profile == dv_profile)
    if dv7_only:
        # DV7 files, the ones already converted from DV7, and files the scan
        # couldn't read (so the user can see which ones are damaged).
        q = q.filter(
            (db_models.LibraryMediaFile.dv_profile == 7)
            | (db_models.LibraryMediaFile.status == library_catalog.CONVERTED)
            | (db_models.LibraryMediaFile.status == library_catalog.UNREADABLE)
        )
    rows = q.order_by(db_models.LibraryMediaFile.path).offset(offset).limit(limit).all()
    return [
        Dv7LibraryFile(
            id=r.id, path=r.path, library_root=r.library_root, size=r.size,
            video_codec=r.video_codec, width=r.width, height=r.height,
            dv_profile=r.dv_profile, duration=r.duration, status=r.status,
            progress=r.progress, error=r.error,
            scanned_at=r.scanned_at.isoformat() if r.scanned_at else None,
            converted_at=r.converted_at.isoformat() if r.converted_at else None,
        )
        for r in rows
    ]


@router.post("/library/files/{file_id}/retry")
def retry_file(file_id: str, db: Session = Depends(get_db)) -> dict:
    if not settings.get_dv7_conversion()["enabled"]:
        raise HTTPException(409, detail="Dolby Vision conversion is turned off.")
    requeued = library_catalog.retry_catalog_file(db, file_id)
    if not requeued:
        raise HTTPException(409, detail="Only a failed or skipped Dolby Vision 7 file can be retried.")
    from workers.tasks import convert_library_file
    convert_library_file.apply_async(args=[requeued])
    return {"queued": True}
