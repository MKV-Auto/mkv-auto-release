"""Dolby Vision 7 -> 8.1 conversion: settings, library catalog and queue (#894).

    GET  /system/dv7/config                 settings + per-folder checks
    PUT  /system/dv7/config                 update settings (folders validated)
    POST /system/dv7/scan                   catalog the library now
    GET  /system/dv7/library                counts by status
    GET  /system/dv7/library/files          catalog rows (filter by status / DV7)
    POST /system/dv7/library/files/{id}/retry  re-queue a failed/skipped file

Everything is disabled by default. Scanning and converting only happen while
the master switch is on.
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from api import models as db_models
from api.database import get_db
from core import library_catalog
from core import settings

router = APIRouter(prefix="/system/dv7", tags=["dv7"])


class Dv7Folder(BaseModel):
    path: str
    ok: bool
    problem: Optional[str] = None


class Dv7Config(BaseModel):
    enabled: bool
    convert_new_rips: bool
    library_paths: List[str]
    folders: List[Dv7Folder] = []


class Dv7ConfigUpdate(BaseModel):
    enabled: Optional[bool] = None
    convert_new_rips: Optional[bool] = None
    library_paths: Optional[List[str]] = None


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


def _config_out() -> Dv7Config:
    cfg = settings.get_dv7_conversion()
    folders = []
    for p in cfg["library_paths"]:
        problem = library_catalog.library_path_problem(p)
        folders.append(Dv7Folder(path=p, ok=problem is None, problem=problem))
    return Dv7Config(**cfg, folders=folders)


def _dispatch_scan() -> None:
    from workers.tasks import library_scan
    library_scan.apply_async()


@router.get("/config", response_model=Dv7Config)
def get_dv7_config() -> Dv7Config:
    return _config_out()


@router.put("/config", response_model=Dv7Config)
def update_dv7_config(payload: Dv7ConfigUpdate) -> Dv7Config:
    """Save settings. A folder that doesn't exist, isn't a mapped mount or
    overlaps the app's data is rejected with the reason. Turning the feature
    on (or changing folders while on) starts a scan."""
    if payload.library_paths is not None:
        problems = []
        for p in payload.library_paths:
            if not str(p).strip():
                continue
            problem = library_catalog.library_path_problem(str(p).strip())
            if problem:
                problems.append(problem)
        if problems:
            raise HTTPException(400, detail=" ".join(problems))
    before = settings.get_dv7_conversion()
    after = settings.set_dv7_conversion(
        enabled=payload.enabled,
        convert_new_rips=payload.convert_new_rips,
        library_paths=payload.library_paths,
    )
    turned_on = after["enabled"] and not before["enabled"]
    folders_changed = after["enabled"] and after["library_paths"] != before["library_paths"]
    if (turned_on or folders_changed) and after["library_paths"]:
        _dispatch_scan()
    return _config_out()


@router.post("/scan")
def scan_now() -> dict:
    cfg = settings.get_dv7_conversion()
    if not cfg["enabled"]:
        raise HTTPException(409, detail="Dolby Vision conversion is turned off.")
    if not cfg["library_paths"]:
        raise HTTPException(409, detail="Add a library folder first.")
    _dispatch_scan()
    return {"queued": True}


@router.get("/library")
def library_summary(db: Session = Depends(get_db)) -> dict:
    return library_catalog.catalog_summary(db)


@router.get("/library/files", response_model=List[Dv7LibraryFile])
def library_files(
    status: Optional[str] = None,
    dv7_only: bool = True,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
) -> List[Dv7LibraryFile]:
    q = db.query(db_models.LibraryMediaFile)
    if status:
        q = q.filter(db_models.LibraryMediaFile.status == status)
    if dv7_only:
        # DV7 files, plus the ones already converted from DV7.
        q = q.filter(
            (db_models.LibraryMediaFile.dv_profile == 7)
            | (db_models.LibraryMediaFile.status == library_catalog.CONVERTED)
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
