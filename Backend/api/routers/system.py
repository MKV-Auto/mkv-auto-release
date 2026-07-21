import asyncio
import json
import logging
import shutil
import os
import tempfile
from pathlib import Path
from fastapi import APIRouter, HTTPException, UploadFile, Request
from fastapi.responses import FileResponse

from api.schemas import (
    MakeMKVInfo,
    MakeMKVUpdateRequest,
    MakeMKVUpdateResponse,
    MakeMKVUpdateJobResponse,
    MakeMKVUpdateJobStatus,
    MakeMKVUpdateActiveResponse,
    LatestVersionResponse,
    MakeMKVRegistrationStatus,
    MakeMKVRegistrationRequest,
    StorageInfo,
    StorageSummary,
    StorageDirEntry,
    MkdirRequest,
    RsyncConfig,
    RsyncConfigResponse,
    PreviewSettings,
    DiscordSettings,
    MediaServerSettings,
    DiscDbLookupSettings,
    TmdbConfigRequest,
    TmdbConfigResponse,
    TransferConfigCreate,
    TransferConfigUpdate,
    TransferConfigSummary,
    TransferConfigRecord,
    ValidationResult,
    TransferHistorySummary,
    TransferStatistics,
    HealthCheckResult,
    TransferHealthStatus,
    ImportSummary,
)
from core.makemkv_updater import (
    get_installed_version,
    get_makemkvcon_metadata,
    update_makemkv,
    MakeMKVUpdateError,
    fetch_latest_version,
    get_registration_status,
    set_registration_key,
    validate_makemkv_installation,
)
from core.makemkv_update_jobs import start_update_job, get_job, get_active_job
from core.utils import (
    get_mkvauto_root,
    get_mkvauto_data,
    is_dev_mode,
    get_discdb_repo_url,
    get_discdb_repo_branch,
    get_discdb_repo_path,
    get_export_root,
)
from core import makemkv_state
from core.transfer.protocols import rsync as rsync_transfer
from core import preview_config
from core import discord_config
from core import settings
from core.transfer import service as transfer_service
from core.utils import notify_discord
from core.transfer.utils import history as transfer_history
from core.transfer import monitoring as transfer_health
from core import path_templates
from api.database import get_db
from api import export_import
from sqlalchemy.orm import Session
from fastapi import Depends
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel
from core.logging_utils import get_logger
from core.log_file_config import LOG_ROTATE_BACKUP_COUNT, LOG_ROTATE_MAX_BYTES
from logging.handlers import RotatingFileHandler

router = APIRouter(prefix="/system", tags=["system"])
log = get_logger("api.routers.system")


# Path to the served frontend's index.html. nginx serves the SPA from
# /app/frontend/dist/disc-ripper-ui/browser/, which is a bind mount from
# the host's Frontend/dist/disc-ripper-ui/browser/ in the docker dev
# setup (see scripts/mkv-lib/docker.sh:start_container). Used by the
# `/frontend-version` endpoint to surface a hash the SPA polls for so
# rebuilds can trigger an auto-reload of open tabs without the user
# having to hard-refresh manually.
_FRONTEND_INDEX_HTML = Path("/app/frontend/dist/disc-ripper-ui/browser/index.html")


def _frontend_version_hash() -> str:
    """SHA-256 prefix of the served index.html. Changes on every
    production frontend build (Angular rewrites the chunk hashes
    referenced by index.html, so the file content changes). Returns
    empty string when the file isn't readable — e.g. running pytest
    without the bundled frontend on disk."""
    import hashlib
    try:
        return hashlib.sha256(_FRONTEND_INDEX_HTML.read_bytes()).hexdigest()[:16]
    except (FileNotFoundError, OSError, PermissionError):
        return ""


@router.get("/frontend-version")
async def get_frontend_version() -> dict:
    """Hash of the served `index.html`. The SPA polls this on a timer
    and reloads itself when the hash changes — gives developers an
    auto-refresh on `npm run build` inside the docker dev container
    instead of having to hard-refresh every tab manually.

    Public endpoint (no devmode gate) so the same mechanism can also
    nudge production users to reload after a deploy. Whether the
    reload is automatic (dev) or prompted via toast (prod) is a
    frontend choice driven by `/system/devmode`.
    """
    return {"version": _frontend_version_hash()}


@router.get("/update-status")
def get_update_status() -> dict:
    """Running version vs the newest published release (#699).

    Consults the public mkv-auto-release GitHub Releases feed, cached
    in-process for 6h, 5s timeout, fail-silent. Sync handler on purpose:
    the outbound request is blocking, so FastAPI runs it in the
    threadpool instead of stalling the event loop. Only ever invoked by
    an active browser session — the backend never phones home on its own.
    """
    from core import update_checker

    return update_checker.get_update_status()


@router.get("/makemkv", response_model=MakeMKVInfo)
async def makemkv_info() -> MakeMKVInfo:
    loop = asyncio.get_running_loop()
    version = await loop.run_in_executor(None, get_installed_version)
    meta = await loop.run_in_executor(None, get_makemkvcon_metadata)
    return MakeMKVInfo(
        version=version,
        binary_path=meta.get("binary_path"),
        resolved_path=meta.get("resolved_path"),
        binary_sha256=meta.get("binary_sha256"),
        binary_mtime=meta.get("binary_mtime"),
    )


def _makemkv_health_payload_sync() -> dict:
    """Build makemkv health dict including disc workflow gating (sync, for executor)."""
    from core.startup_discs import disc_workflow_block_fields
    from core import makemkv_predownload_state

    validation = validate_makemkv_installation()
    expired, _, _ = get_registration_status()
    wf = disc_workflow_block_fields(validation, expired)
    return {
        "installed": validation["is_valid"],
        "valid": validation["is_valid"],
        "can_rip": validation["can_rip"],
        "version": validation["installed_version"],
        "missing_components": validation["missing_components"],
        "error": validation["error_message"],
        "binary_path": validation["binary_path"],
        "download": makemkv_predownload_state.snapshot(),
        **wf,
    }


@router.get("/makemkv/health")
async def makemkv_health() -> dict:
    """
    Check MakeMKV installation health.
    Returns validation status including whether the installation is complete and functional.
    Includes disc_workflow_blocked / disc_workflow_block_reason for rip/disc UX.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _makemkv_health_payload_sync)


@router.get("/makemkv/eula")
async def makemkv_eula():
    """
    Serve the End User License Agreement text extracted from the pre-downloaded
    MakeMKV source tarball (#625). Linked from the Setup Assistant so the user
    can review the actual license before clicking Install. 404 when the
    pre-download hasn't completed yet.
    """
    from fastapi.responses import PlainTextResponse
    from core import makemkv_predownload_state
    from core.makemkv_updater import predownload_dir, PREDOWNLOAD_EULA_NAME

    snap = makemkv_predownload_state.snapshot()
    version = snap.get("version")
    if snap.get("state") != "ready" or not version:
        raise HTTPException(
            status_code=404,
            detail="MakeMKV EULA not yet available — source pre-download in progress or not run.",
        )
    eula_path = predownload_dir(version) / PREDOWNLOAD_EULA_NAME
    if not eula_path.exists():
        raise HTTPException(
            status_code=404,
            detail="MakeMKV EULA text file missing from source cache.",
        )
    try:
        text = eula_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read EULA: {exc}") from exc
    return PlainTextResponse(
        text,
        headers={
            "Cache-Control": "private, max-age=0, no-store",
            "Content-Disposition": "inline",
        },
    )


@router.get("/health")
async def system_health() -> dict:
    """
    Get overall system health including MakeMKV, workers, and storage.
    Does not use the database. All blocking work (MakeMKV check, Celery inspect,
    disk usage) runs in a thread via run_in_executor so the event loop stays free
    and other requests (e.g. GET /system/setup/status) can be handled while health runs.
    """
    loop = asyncio.get_running_loop()
    makemkv_health_data, worker_health_data, storage_health_data = await loop.run_in_executor(
        None, _health_check_sync
    )
    return {
        "makemkv": makemkv_health_data,
        "workers": worker_health_data,
        "storage": storage_health_data,
    }


@router.get("/makemkv/latest", response_model=LatestVersionResponse)
async def makemkv_latest() -> LatestVersionResponse:
    loop = asyncio.get_running_loop()
    try:
        latest = await loop.run_in_executor(None, fetch_latest_version)
        return LatestVersionResponse(version=latest)
    except MakeMKVUpdateError as exc:
        log.warning("Failed to fetch latest MakeMKV version: %s", exc)
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.post("/makemkv/update", response_model=MakeMKVUpdateResponse)
async def makemkv_update(req: MakeMKVUpdateRequest) -> MakeMKVUpdateResponse:
    loop = asyncio.get_running_loop()

    try:
        result = await loop.run_in_executor(
            None,
            lambda: update_makemkv(
                req.version,
                build_ffmpeg=req.build_ffmpeg,
                install_prefix=req.install_prefix,
                work_dir=req.work_dir,
            ),
        )
    except MakeMKVUpdateError as exc:
        log.exception("MakeMKV update failed")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover
        log.exception("Unexpected failure updating MakeMKV")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    # clear any prior disable flag on success
    makemkv_state.clear_disabled()
    return MakeMKVUpdateResponse(
        version=result.get("version", req.version),
        logs=[line for line in result.get("logs", []) if line],
        ffmpeg_built=result.get("ffmpeg_built", False),
    )

@router.post("/makemkv/update/start", response_model=MakeMKVUpdateJobResponse)
async def makemkv_update_start(req: MakeMKVUpdateRequest) -> MakeMKVUpdateJobResponse:
    # If an install is already running, return its job_id so the client subscribes to it (no duplicate job).
    active = get_active_job()
    if active:
        job_id, _ = active
        return MakeMKVUpdateJobResponse(jobId=job_id)
    loop = asyncio.get_running_loop()
    job_id = start_update_job(
        version=req.version,
        build_ffmpeg=True,  # Always build FFmpeg
        ffmpeg_advanced_features=req.ffmpeg_advanced_features,
        install_prefix=req.install_prefix,
        work_dir=req.work_dir,
        loop=loop,
    )
    return MakeMKVUpdateJobResponse(jobId=job_id)


@router.get("/makemkv/update/job/{job_id}", response_model=MakeMKVUpdateJobStatus)
async def get_makemkv_update_job(job_id: str) -> MakeMKVUpdateJobStatus:
    """
    Get current status of a MakeMKV update job.
    Returns job status, logs, and completion state.
    """
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return MakeMKVUpdateJobStatus(
        jobId=job_id,
        status=job.status,
        logs=job.logs,
        error=job.error,
        version=job.version,
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


@router.get("/makemkv/update/active", response_model=MakeMKVUpdateActiveResponse)
async def get_makemkv_update_active() -> MakeMKVUpdateActiveResponse:
    """
    Return the current in-progress MakeMKV update job, if any.
    Used by the frontend to reattach after refresh or to avoid starting a second install.
    """
    active = get_active_job()
    if not active:
        return MakeMKVUpdateActiveResponse(active=False)
    job_id, job = active
    return MakeMKVUpdateActiveResponse(
        active=True,
        jobId=job_id,
        status=job.status,
        logs=job.logs,
        error=job.error,
        version=job.version,
        createdAt=job.created_at,
        updatedAt=job.updated_at,
    )


@router.get("/makemkv/registration", response_model=MakeMKVRegistrationStatus)
async def makemkv_registration_status() -> MakeMKVRegistrationStatus:
    loop = asyncio.get_running_loop()
    expired, msg, key = await loop.run_in_executor(None, get_registration_status)
    return MakeMKVRegistrationStatus(expired=expired, message=msg, currentKey=key)


@router.post("/makemkv/register", response_model=MakeMKVRegistrationStatus)
async def makemkv_register(req: MakeMKVRegistrationRequest) -> MakeMKVRegistrationStatus:
    loop = asyncio.get_running_loop()
    
    
    # Validate key with makemkvcon reg
    try:
        success, msg = await loop.run_in_executor(None, set_registration_key, req.key)
    except MakeMKVUpdateError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    
    # Save to app settings
    try:
        settings.save_settings({"makemkv_registration_key": req.key})
    except Exception:
        pass
    
    # Check current status
    expired, status_msg, key = await loop.run_in_executor(None, get_registration_status)

    # Retry drive warmup only if post-install scan failed until a key was registered
    try:
        from core.startup_discs import (
            drive_warmup_pending_after_key,
            run_startup_drive_warmup_if_makemkv_ready,
        )

        if drive_warmup_pending_after_key():
            await loop.run_in_executor(None, run_startup_drive_warmup_if_makemkv_ready)
    except Exception as wexc:
        log.warning("Post-registration drive warmup failed: %s", wexc, exc_info=True)

    return MakeMKVRegistrationStatus(expired=expired, message=status_msg or msg, currentKey=key)


def _health_check_sync() -> tuple:
    """
    Run all blocking health checks in one sync function for use from system_health.
    Returns (makemkv_validation, worker_health_data, storage_health_data) so the
    async handler can return without blocking the event loop.
    """
    makemkv_health_data = _makemkv_health_payload_sync()
    try:
        from workers.tasks import check_worker_health
        worker_health_data = check_worker_health()
    except Exception as exc:
        log.error("Error checking worker health: %s", exc, exc_info=True)
        worker_health_data = {
            "status": "error",
            "worker_count": 0,
            "active_workers": [],
            "issues": [f"Failed to check worker health: {exc}"],
        }
    data_root = get_mkvauto_data()
    storage_info = _usage_for(data_root)
    storage_health_data = {
        "path": storage_info.path,
        "total_gb": round(storage_info.total / (1024**3), 2),
        "free_gb": round(storage_info.free / (1024**3), 2),
        "used_percent": round((storage_info.used / storage_info.total * 100), 1) if storage_info.total > 0 else 0,
    }
    return (makemkv_health_data, worker_health_data, storage_health_data)


def _usage_for(path: Path) -> StorageInfo:
    requested = path
    target = requested
    while not target.exists() and target != target.parent:
        target = target.parent
    if not target.exists():
        target = Path("/")

    usage = shutil.disk_usage(target)
    return StorageInfo(
        path=str(target),
        total=usage.total,
        used=usage.used,
        free=usage.free,
    )

def _ensure_dir(path: Path) -> Path:
    try:
        path.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return path

@router.get("/storage", response_model=StorageInfo)
async def storage_info(path: str | None = None) -> StorageInfo:
    """
    Return disk usage for the given path (defaults to MKVAUTO_DATA).
    If the path does not exist, fallback to the nearest existing parent.
    """
    requested = Path(path).expanduser() if path else get_mkvauto_data()
    return _usage_for(requested)


@router.get("/storage/summary", response_model=StorageSummary)
async def storage_summary(db: Session = Depends(get_db)) -> StorageSummary:
    data_root = _ensure_dir(get_mkvauto_data())
    
    # Get active transfer config destination if available
    transfer_path = None
    from core.transfer.service import get_active_config, check_storage
    active_config = get_active_config(db)
    
    transfer_root_info = None
    
    if active_config:
        log.debug(f"Active transfer config found: mode={active_config.mode}, transfer_dir={active_config.transfer_dir}, id={active_config.id}")
        if active_config.mode == "local":
            # For local transfers, use the transfer_dir as the destination
            if active_config.transfer_dir and active_config.transfer_dir.strip():
                transfer_path = Path(active_config.transfer_dir).expanduser().resolve()
                log.debug(f"Using active local transfer config destination: {transfer_path}")
            else:
                # Local config but no transfer_dir set, fall back to data root
                log.debug("Active local config has no transfer_dir set, using data root")
                transfer_path = get_mkvauto_data()
        else:
            # For remote transfers (rsync, smb, nfs), use storage detection
            log.debug(f"Active config is remote ({active_config.mode}), checking remote storage")
            storage_info, error = check_storage(db, active_config)
            
            # Build remote path from config
            config_data = active_config.config_data or {}
            if active_config.mode == "smb":
                remote_path = f"smb://{config_data.get('host', '')}/{config_data.get('share', '')}"
            elif active_config.mode == "nfs":
                remote_path = f"nfs://{config_data.get('server', '')}{config_data.get('export_path', '')}"
            elif active_config.mode == "rsync":
                remote_path = f"{config_data.get('user', '')}@{config_data.get('host', '')}:{config_data.get('path', '')}"
            else:
                remote_path = f"{active_config.mode}://unknown"
            
            if error:
                log.warning(f"Could not check remote storage for {active_config.mode} config {active_config.id}: {error}")
                # Still show remote destination path even if we can't check storage
                # Include error in path so user knows what went wrong
                from api.schemas import StorageInfo
                error_path = f"{remote_path} (Error: {error})"
                transfer_root_info = StorageInfo(
                    path=error_path,
                    total=0,
                    used=0,
                    free=0,
                )
            elif storage_info:
                # Convert storage_info dict to StorageInfo
                from api.schemas import StorageInfo
                transfer_root_info = StorageInfo(
                    path=storage_info.get("path", remote_path),  # Use detected path or fallback to constructed path
                    total=storage_info.get("total", 0),
                    used=storage_info.get("used", 0),
                    free=storage_info.get("free", 0),
                )
                log.debug(f"Remote storage info retrieved: {transfer_root_info.free / (1024**3):.2f} GB free")
            else:
                # No storage info and no error (shouldn't happen, but handle it)
                log.warning(f"No storage info returned for remote config")
                from api.schemas import StorageInfo
                transfer_root_info = StorageInfo(
                    path=remote_path,
                    total=0,
                    used=0,
                    free=0,
                )
    else:
        # No active config, fallback to legacy env var or data root
        log.debug("No active transfer config found, using fallback")
        transfer_path = Path(os.getenv("MAKEMKV_TRANSFER_DIR", get_mkvauto_data()))
    
    # If we didn't get remote storage info, use local path
    if transfer_root_info is None:
        transfer_root = _ensure_dir(transfer_path) if transfer_path else data_root
        log.debug(f"Transfer usage path: {transfer_root}")
        transfer_root_info = _usage_for(transfer_root)
    
    return StorageSummary(
        data_root=_usage_for(data_root),
        transfer_root=transfer_root_info,
    )


@router.get("/storage/listdir", response_model=list[StorageDirEntry])
async def list_directory(path: str | None = None) -> list[StorageDirEntry]:
    """
    Return a simple directory listing for the given path (directories only for safety).
    """
    target = Path(path).expanduser().resolve() if path else get_mkvauto_data().resolve()
    if not target.exists():
        raise HTTPException(400, detail=f"Path not found: {target}")
    if not target.is_dir():
        raise HTTPException(400, detail=f"Not a directory: {target}")

    entries: list[StorageDirEntry] = []
    try:
        for entry in target.iterdir():
            if entry.is_dir():
                entries.append(StorageDirEntry(name=entry.name, path=str(entry), is_dir=True))
    except PermissionError as exc:
        raise HTTPException(403, detail=f"Permission denied listing {target}: {exc}") from exc

    # sort by name
    entries.sort(key=lambda x: x.name.lower())
    return entries


@router.post("/storage/mkdir", response_model=StorageDirEntry)
async def make_directory(req: MkdirRequest) -> StorageDirEntry:
    base = Path(req.path).expanduser().resolve() if req.path else get_mkvauto_data().resolve()
    target = (base / req.name).resolve()
    if not str(target).startswith(str(base)):
        raise HTTPException(400, detail="Invalid path")
    try:
        target.mkdir(parents=True, exist_ok=True)
    except PermissionError as exc:
        raise HTTPException(403, detail=f"Permission denied: {exc}") from exc
    except Exception as exc:
        raise HTTPException(400, detail=str(exc)) from exc
    return StorageDirEntry(name=target.name, path=str(target), is_dir=True)


@router.get("/transfer/rsync/config", response_model=RsyncConfigResponse)
async def get_rsync_config() -> RsyncConfigResponse:
    cfg, has_key = rsync_transfer.load_config()
    return RsyncConfigResponse(config=cfg, hasKey=has_key)


@router.post("/transfer/rsync/config", response_model=RsyncConfigResponse)
async def save_rsync_config(req: RsyncConfig) -> RsyncConfigResponse:
    if not req.host or not req.user or not req.path:
        raise HTTPException(400, detail="host, user, and path are required")
    rsync_transfer.save_config(rsync_transfer.RsyncConfig(
        host=req.host,
        user=req.user,
        path=req.path,
        port=req.port or 22,
        bwlimit=req.bwlimit,
    ))
    _, has_key = rsync_transfer.load_config()
    return RsyncConfigResponse(config=req, hasKey=has_key)


@router.post("/transfer/rsync/key", response_model=RsyncConfigResponse)
async def upload_rsync_key(file: UploadFile) -> RsyncConfigResponse:
    data = await file.read()
    if not data:
        raise HTTPException(400, detail="Empty key file")
    rsync_transfer.save_key(data)
    cfg, has_key_from_load = rsync_transfer.load_config()
    return RsyncConfigResponse(config=cfg, hasKey=True)


@router.delete("/transfer/rsync/key", response_model=RsyncConfigResponse)
async def delete_rsync_key() -> RsyncConfigResponse:
    rsync_transfer.delete_key()
    cfg, _ = rsync_transfer.load_config()
    return RsyncConfigResponse(config=cfg, hasKey=False)


@router.get("/workers/health")
async def get_worker_health():
    """
    Get health status of Celery workers.
    Returns information about active workers, registered workers, and any issues detected.
    """
    try:
        from workers.tasks import check_worker_health
        health = check_worker_health()
        return {
            "status": health["status"],
            "worker_count": health["worker_count"],
            "active_workers": health["active_workers"],
            "issues": health["issues"],
        }
    except Exception as exc:
        log.error(f"Error checking worker health: {exc}", exc_info=True)
        raise HTTPException(500, detail=f"Failed to check worker health: {exc}")

# Transfer config endpoints
def _capabilities_from_config_data(config_data: Optional[dict]):
    """Extract cached capabilities dict from ``config.config_data`` if any.

    Returns ``None`` when never probed so the API surface distinguishes
    "not probed" from "probed and pessimistic" (the frontend uses this to
    show a spinner vs. a red pill).
    """
    if not isinstance(config_data, dict):
        return None
    caps = config_data.get("capabilities")
    if not isinstance(caps, dict):
        return None
    return caps


@router.get("/transfer/configs", response_model=List[TransferConfigSummary])
async def list_transfer_configs(db: Session = Depends(get_db)) -> List[TransferConfigSummary]:
    """List all transfer configs with health status."""
    from api import models
    configs = db.query(models.TransferConfig).order_by(models.TransferConfig.created_at.desc()).all()

    result = []
    for config in configs:
        # Get health status
        health_status = transfer_health.get_health_status(db, config.id)
        overall_status = health_status.get("overall", {}).get("status", "unknown")

        result.append(TransferConfigSummary(
            id=config.id,
            mode=config.mode,
            name=config.name,
            is_active=config.is_active,
            transfer_dir=config.transfer_dir,
            path_template=config.path_template,
            path_template_schema_version=getattr(config, "path_template_schema_version", None),
            conflict_resolution=config.conflict_resolution,
            health_check_interval_minutes=config.health_check_interval_minutes,
            health_status=overall_status,
            capabilities=_capabilities_from_config_data(config.config_data),
            created_at=config.created_at.isoformat() if config.created_at else "",
            updated_at=config.updated_at.isoformat() if config.updated_at else "",
        ))

    return result


def _validate_transfer_config_required(mode: str, transfer_dir: str | None, config_data: dict | None) -> str | None:
    """Validate required fields for the given transfer mode. Returns error message or None."""
    # Transfer Path is required for all modes (single cohesive field)
    if not transfer_dir or not str(transfer_dir).strip():
        return "Transfer Path is required. Please set a destination path."
    data = config_data or {}
    if mode == "rsync":
        if not data.get("host") or not str(data.get("host", "")).strip():
            return "Rsync host is required."
        if not data.get("user") or not str(data.get("user", "")).strip():
            return "Rsync user is required."
        return None
    if mode == "smb":
        if not data.get("host") or not str(data.get("host", "")).strip():
            return "SMB host is required."
        if not data.get("share") or not str(data.get("share", "")).strip():
            return "SMB share is required."
        return None
    if mode == "nfs":
        if not data.get("server") or not str(data.get("server", "")).strip():
            return "NFS server is required."
        if not data.get("export_path") or not str(data.get("export_path", "")).strip():
            return "NFS export path is required."
        return None
    return None


@router.post("/transfer/configs", response_model=TransferConfigRecord)
async def create_transfer_config(
    req: TransferConfigCreate,
    db: Session = Depends(get_db)
) -> TransferConfigRecord:
    """Create a new transfer config."""
    err = _validate_transfer_config_required(
        req.mode,
        getattr(req, "transfer_dir", None),
        req.config_data,
    )
    if err:
        raise HTTPException(status_code=422, detail=err)
    # For remote modes, sync Transfer Path into config_data.path so protocols see it
    config_data = dict(req.config_data or {})
    if req.mode in ("rsync", "smb", "nfs") and getattr(req, "transfer_dir", None):
        config_data["path"] = (req.transfer_dir or "").strip()
    extra = req.model_dump(exclude_unset=True)
    for key in ("mode", "name", "config_data", "credentials"):
        extra.pop(key, None)
    if extra.get("path_template") is not None:
        extra["path_template_schema_version"] = path_templates.PATH_TEMPLATE_SCHEMA_VERSION
    config = transfer_service.create_config(
        db,
        req.mode,
        req.name,
        config_data,
        req.credentials,
        extra_attrs=extra or None,
    )

    return TransferConfigRecord(
        id=config.id,
        mode=config.mode,
        name=config.name,
        is_active=config.is_active,
        transfer_dir=config.transfer_dir,
        output_dir=config.output_dir,
        path_template=config.path_template,
        path_template_schema_version=getattr(config, "path_template_schema_version", None),
        config_data=config.config_data,
        conflict_resolution=config.conflict_resolution,
        health_check_interval_minutes=config.health_check_interval_minutes,
        capabilities=_capabilities_from_config_data(config.config_data),
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.get("/transfer/configs/{config_id}", response_model=TransferConfigRecord)
async def get_transfer_config(
    config_id: str,
    db: Session = Depends(get_db)
) -> TransferConfigRecord:
    """Get a specific transfer config."""
    from api import models
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise HTTPException(404, detail="Transfer config not found")

    return TransferConfigRecord(
        id=config.id,
        mode=config.mode,
        name=config.name,
        is_active=config.is_active,
        transfer_dir=config.transfer_dir,
        output_dir=config.output_dir,
        path_template=config.path_template,
        path_template_schema_version=getattr(config, "path_template_schema_version", None),
        config_data=config.config_data,
        conflict_resolution=config.conflict_resolution,
        health_check_interval_minutes=config.health_check_interval_minutes,
        capabilities=_capabilities_from_config_data(config.config_data),
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.put("/transfer/configs/{config_id}", response_model=TransferConfigRecord)
async def update_transfer_config(
    config_id: str,
    req: TransferConfigUpdate,
    db: Session = Depends(get_db)
) -> TransferConfigRecord:
    """Update a transfer config."""
    updates = req.model_dump(exclude_unset=True)

    # Validate required fields for the mode (effective state after update)
    from api import models
    existing = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not existing:
        raise HTTPException(status_code=404, detail="Transfer config not found")
    mode = existing.mode  # mode is not changeable on update
    transfer_dir = updates.get("transfer_dir", existing.transfer_dir)
    config_data = updates.get("config_data", existing.config_data)
    err = _validate_transfer_config_required(mode, transfer_dir, config_data)
    if err:
        raise HTTPException(status_code=422, detail=err)

    # For remote modes, sync Transfer Path into config_data.path
    if mode in ("rsync", "smb", "nfs") and "transfer_dir" in updates:
        config_data = dict(updates.get("config_data") or existing.config_data or {})
        config_data["path"] = (updates["transfer_dir"] or "").strip()
        updates["config_data"] = config_data

    # Handle credentials separately
    credentials = updates.pop("credentials", None)
    if "path_template" in updates and updates.get("path_template") is not None:
        updates["path_template_schema_version"] = path_templates.PATH_TEMPLATE_SCHEMA_VERSION
    elif "path_template" in updates and updates.get("path_template") is None:
        updates["path_template_schema_version"] = None

    config = transfer_service.update_config(db, config_id, updates)
    
    # Update credentials if provided
    if credentials:
        from core.transfer.utils.credentials import encrypt_and_store_credentials
        encrypt_and_store_credentials(db, config_id, credentials)

    return TransferConfigRecord(
        id=config.id,
        mode=config.mode,
        name=config.name,
        is_active=config.is_active,
        transfer_dir=config.transfer_dir,
        output_dir=config.output_dir,
        path_template=config.path_template,
        path_template_schema_version=getattr(config, "path_template_schema_version", None),
        config_data=config.config_data,
        conflict_resolution=config.conflict_resolution,
        health_check_interval_minutes=config.health_check_interval_minutes,
        capabilities=_capabilities_from_config_data(config.config_data),
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.delete("/transfer/configs/{config_id}")
async def delete_transfer_config(
    config_id: str,
    db: Session = Depends(get_db)
):
    """Delete a transfer config."""
    try:
        transfer_service.delete_config(db, config_id)
    except ValueError as e:
        msg = str(e)
        if "not found" in msg.lower():
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True}


@router.post("/transfer/configs/{config_id}/activate", response_model=TransferConfigRecord)
async def activate_transfer_config(
    config_id: str,
    db: Session = Depends(get_db)
) -> TransferConfigRecord:
    """Activate a transfer config (deactivates all others). #635 commit B:
    fire the capability probe after activation succeeds so the strategy
    selector has data before the next transfer runs."""
    config = transfer_service.activate_config(db, config_id)

    try:
        from workers.tasks import probe_transfer_capabilities
        probe_transfer_capabilities.delay(config.id)
    except Exception as exc:
        log.warning("Failed to enqueue probe_transfer_capabilities on activate: %s", exc)

    return TransferConfigRecord(
        id=config.id,
        mode=config.mode,
        name=config.name,
        is_active=config.is_active,
        transfer_dir=config.transfer_dir,
        output_dir=config.output_dir,
        path_template=config.path_template,
        path_template_schema_version=getattr(config, "path_template_schema_version", None),
        config_data=config.config_data,
        conflict_resolution=config.conflict_resolution,
        health_check_interval_minutes=config.health_check_interval_minutes,
        capabilities=_capabilities_from_config_data(config.config_data),
        created_at=config.created_at.isoformat() if config.created_at else "",
        updated_at=config.updated_at.isoformat() if config.updated_at else "",
    )


@router.post("/transfer/configs/{config_id}/probe-capabilities", status_code=202)
async def probe_transfer_config_capabilities(
    config_id: str,
    db: Session = Depends(get_db),
):
    """Enqueue a capability probe for the given config (#635 commit B).

    Returns 202 immediately; the frontend receives the result via the
    ``transfer_config_capabilities_updated`` websocket event when the
    probe completes.
    """
    from api import models
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise HTTPException(status_code=404, detail="Transfer config not found")
    try:
        from workers.tasks import probe_transfer_capabilities
        probe_transfer_capabilities.delay(config_id)
    except Exception as exc:
        log.warning("Failed to enqueue probe_transfer_capabilities: %s", exc)
        raise HTTPException(status_code=500, detail=f"Failed to enqueue probe: {exc}")
    return {"success": True, "config_id": config_id, "queued": True}


@router.post("/transfer/configs/{config_id}/validate", response_model=ValidationResult)
async def validate_transfer_config(
    config_id: str,
    db: Session = Depends(get_db)
) -> ValidationResult:
    """Test connection for a transfer config."""
    success, message = transfer_service.validate_connection(db, config_id)
    return ValidationResult(success=success, message=message, errors=[message] if not success else None)


@router.post("/transfer/configs/{config_id}/health-check", response_model=TransferHealthStatus)
async def trigger_health_check(
    config_id: str,
    db: Session = Depends(get_db)
) -> TransferHealthStatus:
    """Manually trigger a health check for a transfer config."""
    from api import models
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise HTTPException(404, detail="Transfer config not found")
    
    results = transfer_health.check_destination_health(config)
    transfer_health.record_health_check(db, config_id, results)
    
    return TransferHealthStatus(
        overall=HealthCheckResult(**results.get("overall", {})),
        connectivity=HealthCheckResult(**results.get("connectivity", {})),
        authentication=HealthCheckResult(**results.get("authentication", {})),
        permissions=HealthCheckResult(**results.get("permissions", {})),
        space=HealthCheckResult(**results.get("space", {})),
    )


@router.get("/transfer/configs/{config_id}/health", response_model=TransferHealthStatus)
async def get_transfer_health(
    config_id: str,
    db: Session = Depends(get_db)
) -> TransferHealthStatus:
    """Get health status and history for a transfer config."""
    status = transfer_health.get_health_status(db, config_id)
    
    return TransferHealthStatus(
        overall=HealthCheckResult(**status.get("overall", {})) if status.get("overall") else None,
        connectivity=HealthCheckResult(**status.get("connectivity", {})) if status.get("connectivity") else None,
        authentication=HealthCheckResult(**status.get("authentication", {})) if status.get("authentication") else None,
        permissions=HealthCheckResult(**status.get("permissions", {})) if status.get("permissions") else None,
        space=HealthCheckResult(**status.get("space", {})) if status.get("space") else None,
    )


@router.post("/transfer/configs/{config_id}/test-template", response_model=dict)
async def test_path_template(
    config_id: str,
    sample_data: dict,
    db: Session = Depends(get_db)
) -> dict:
    """Test a path template with sample data."""
    from api import models
    config = db.query(models.TransferConfig).filter(models.TransferConfig.id == config_id).first()
    if not config:
        raise HTTPException(404, detail="Transfer config not found")
    
    if not config.path_template:
        raise HTTPException(400, detail="No path template configured")
    
    resolved = path_templates.resolve_template(config.path_template, sample_data)
    return {"resolved": resolved}


@router.get("/transfer/history", response_model=List[TransferHistorySummary])
async def get_transfer_history(
    job_id: Optional[str] = None,
    config_id: Optional[str] = None,
    limit: int = 100,
    db: Session = Depends(get_db)
) -> List[TransferHistorySummary]:
    """Get transfer history.

    Resolves human-readable identity (#593) via the eager-loaded
    Job → Disc → Release → Movie chain. Orphan rows (job deleted,
    job_id SET NULL) return NULL identity fields and the UI degrades
    to source-path parsing or the raw UUID.
    """
    history = transfer_history.get_transfer_history(db, job_id, config_id, limit)

    out: List[TransferHistorySummary] = []
    for h in history:
        movie_name: Optional[str] = None
        release_name: Optional[str] = None
        release_year: Optional[int] = None
        disc_name: Optional[str] = None
        disc = h.job.disc if h.job else None
        if disc is not None:
            disc_name = disc.disc_name or disc.info_title or None
            release = disc.release
            if release is not None:
                release_name = release.name or None
                release_year = release.release_year
                movie = release.movie
                if movie is not None:
                    movie_name = movie.name or None
        out.append(
            TransferHistorySummary(
                id=h.id,
                job_id=h.job_id,
                transfer_config_id=h.transfer_config_id,
                mode=h.mode,
                source_path=h.source_path,
                destination_path=h.destination_path,
                status=h.status,
                bytes_transferred=h.bytes_transferred,
                transfer_duration_seconds=h.transfer_duration_seconds,
                average_speed_mbps=h.average_speed_mbps,
                verification_status=h.verification_status,
                was_deduplicated=h.was_deduplicated,
                created_at=h.created_at.isoformat() if h.created_at else "",
                movie_name=movie_name,
                release_name=release_name,
                release_year=release_year,
                disc_name=disc_name,
            )
        )
    return out


@router.get("/transfer/statistics", response_model=TransferStatistics)
async def get_transfer_statistics(
    config_id: Optional[str] = None,
    days: int = 30,
    db: Session = Depends(get_db)
) -> TransferStatistics:
    """Get transfer statistics."""
    stats = transfer_history.get_transfer_statistics(db, config_id, days)
    return TransferStatistics(**stats)


@router.get("/preview/config", response_model=PreviewSettings)
async def get_preview_config() -> PreviewSettings:
    cfg = preview_config.load_preview_config()
    return PreviewSettings(**cfg)


@router.post("/preview/config", response_model=PreviewSettings)
async def save_preview_config(settings: PreviewSettings) -> PreviewSettings:
    # Practical ceiling = server CPU count (what the worker pool can actually
    # use). Reject overruns explicitly so the UI can surface a useful error
    # instead of the value being silently clamped.
    ceiling = max(1, os.cpu_count() or 1)
    if settings.max_parallel > ceiling:
        raise HTTPException(
            status_code=400,
            detail=f"max_parallel={settings.max_parallel} exceeds server ceiling {ceiling} (os.cpu_count())",
        )
    cfg = preview_config.save_preview_config(
        duration_seconds=settings.duration_seconds,
        max_parallel=settings.max_parallel,
        disable_ffmpeg_junk_detection=settings.disable_ffmpeg_junk_detection,
    )
    return PreviewSettings(**cfg)


@router.get("/discord/config", response_model=DiscordSettings)
async def get_discord_config() -> DiscordSettings:
    cfg = discord_config.load_discord_config()
    return DiscordSettings.model_validate(cfg)


@router.post("/discord/config", response_model=DiscordSettings)
async def save_discord_config(settings: DiscordSettings) -> DiscordSettings:
    kwargs = {
        "webhook_url": settings.webhook_url,
        "enabled": settings.enabled,
    }
    if settings.notification_preferences is not None:
        kwargs["notification_preferences"] = settings.notification_preferences.model_dump()
    cfg = discord_config.save_discord_config(**kwargs)
    return DiscordSettings.model_validate(cfg)


@router.get("/tmdb/config", response_model=TmdbConfigResponse)
async def get_tmdb_config() -> TmdbConfigResponse:
    """Current TMDB configuration. Returns the persisted key value so the
    Settings → TMDB input field can pre-populate (#610 — mirrors how MakeMKV
    registration echoes ``currentKey``)."""
    key = settings.get_tmdb_api_key()
    return TmdbConfigResponse(api_key_set=bool(key), api_key=key or None)


@router.post("/tmdb/config", response_model=TmdbConfigResponse)
async def save_tmdb_config(body: TmdbConfigRequest) -> TmdbConfigResponse:
    """Persist or clear the TMDB v3 API key (#369). Empty string or null clears.

    When a key is set (transition from no-key → key), also run a synchronous
    backfill that populates ``disc.disc_info.tmdb_suggestion`` for any unlabeled
    discs sitting in the DB. Without this an existing user who plugs in their
    key would only see suggestions on FUTURE scans — discs already on the
    workbench would silently miss out, which is exactly the surprise that
    motivated this addition.
    """
    had_key_before = bool(settings.get_tmdb_api_key())
    settings.set_tmdb_api_key(body.api_key)
    api_key_set = bool(settings.get_tmdb_api_key())

    backfill: Optional[dict] = None
    if api_key_set and not had_key_before:
        # Only run on the transition into "key configured". Re-saving the same
        # key, or clearing the key, doesn't re-trigger the backfill.
        try:
            from core import disc_manager
            from api import database as _db
            session = _db.SessionLocal()
            try:
                backfill = disc_manager.backfill_tmdb_suggestions_for_unlabeled_discs(session)
            finally:
                session.close()
        except Exception as exc:  # backfill must never fail the save
            log.warning("TMDB backfill after key save failed: %s", exc, exc_info=True)

    # #610: echo the key on POST too so the UI doesn't have to re-fetch GET
    # right after a save just to get the value into the field.
    return TmdbConfigResponse(
        api_key_set=api_key_set,
        api_key=settings.get_tmdb_api_key() or None,
        backfill=backfill,
    )


@router.get("/media-server/config", response_model=MediaServerSettings)
async def get_media_server_config() -> MediaServerSettings:
    """Get library/media-server format (Plex vs Jellyfin) for postprocess paths."""
    return MediaServerSettings(media_server=settings.get_media_server())


@router.post("/media-server/config", response_model=MediaServerSettings)
async def save_media_server_config(payload: MediaServerSettings) -> MediaServerSettings:
    """Set library/media-server format (Plex vs Jellyfin) for postprocess paths."""
    settings.set_media_server(payload.media_server)
    return MediaServerSettings(media_server=settings.get_media_server())


@router.get("/discdb-lookup/config", response_model=DiscDbLookupSettings)
async def get_discdb_lookup_config() -> DiscDbLookupSettings:
    """Copy settings: DiscDB prefill toggle + eject on finish."""
    s = settings.load_settings()
    return DiscDbLookupSettings(
        discdb_miss_workflow_with_prefill=settings.get_discdb_miss_workflow_with_prefill(),
        eject_on_finish=bool(s.get("eject_on_finish", False)),
    )


@router.post("/discdb-lookup/config", response_model=DiscDbLookupSettings)
async def save_discdb_lookup_config(payload: DiscDbLookupSettings) -> DiscDbLookupSettings:
    settings.set_discdb_miss_workflow_with_prefill(payload.discdb_miss_workflow_with_prefill)
    settings.save_settings({"eject_on_finish": payload.eject_on_finish})
    s = settings.load_settings()
    return DiscDbLookupSettings(
        discdb_miss_workflow_with_prefill=settings.get_discdb_miss_workflow_with_prefill(),
        eject_on_finish=bool(s.get("eject_on_finish", False)),
    )


class AutoRipSettings(BaseModel):
    """Auto-rip toggle persisted in settings.json (#331)."""
    auto_rip_enabled: bool


@router.get("/auto-rip/config", response_model=AutoRipSettings)
async def get_auto_rip_config() -> AutoRipSettings:
    return AutoRipSettings(auto_rip_enabled=settings.get_auto_rip_enabled())


@router.post("/auto-rip/config", response_model=AutoRipSettings)
async def save_auto_rip_config(payload: AutoRipSettings) -> AutoRipSettings:
    settings.set_auto_rip_enabled(payload.auto_rip_enabled)
    return AutoRipSettings(auto_rip_enabled=settings.get_auto_rip_enabled())


class SetupStatusResponse(BaseModel):
    """First-time setup status and current wizard step."""
    first_time_setup_complete: bool
    setup_step: int


class SetupProgressUpdate(BaseModel):
    """Request body to persist setup wizard step (1-6)."""
    setup_step: int


@router.get("/setup/status", response_model=SetupStatusResponse)
async def get_setup_status() -> SetupStatusResponse:
    """Get first-time setup completion flag and current setup wizard step."""
    import time
    t0 = time.perf_counter()
    complete = settings.get_first_time_setup_complete()
    step = settings.get_setup_step()
    elapsed = time.perf_counter() - t0
    log.info("get_setup_status: complete=%s step=%s elapsed_sec=%.3f", complete, step, elapsed)
    return SetupStatusResponse(first_time_setup_complete=complete, setup_step=step)


@router.post("/setup/complete", response_model=SetupStatusResponse)
async def mark_setup_complete() -> SetupStatusResponse:
    """Mark first-time setup as complete."""
    settings.set_first_time_setup_complete(True)
    return SetupStatusResponse(
        first_time_setup_complete=settings.get_first_time_setup_complete(),
        setup_step=settings.get_setup_step(),
    )


@router.patch("/setup/progress", response_model=SetupStatusResponse)
async def save_setup_progress(body: SetupProgressUpdate) -> SetupStatusResponse:
    """Persist current setup wizard step (1-6)."""
    step = body.setup_step
    if not isinstance(step, int) or step < 1 or step > 6:
        raise HTTPException(status_code=400, detail="setup_step must be an integer between 1 and 6")
    settings.set_setup_step(step)
    return SetupStatusResponse(
        first_time_setup_complete=settings.get_first_time_setup_complete(),
        setup_step=settings.get_setup_step(),
    )


@router.post("/discord/notify")
async def send_discord_notification(request: Request):
    """
    Send a notification to Discord if configured.
    Called by the frontend when toast notifications are shown.
    """
    try:
        params = request.query_params
        message = params.get("message", "")
        kind = params.get("kind", "info")
    except Exception:
        message = ""
        kind = "info"
    
    if not message:
        return {"status": "error", "message": "Message is required"}
    
    webhook_url = discord_config.get_webhook_url()
    if not webhook_url:
        return {"status": "disabled", "message": "Discord notifications not configured"}
    
    try:
        # Map toast kinds to Discord-friendly emojis
        emoji_map = {
            "success": "✅",
            "error": "❌",
            "warning": "⚠️",
            "info": "ℹ️",
        }
        emoji = emoji_map.get(kind, "")
        formatted_message = f"{emoji} {message}" if emoji else message
        
        notify_discord(webhook_url, formatted_message)
        return {"status": "sent", "message": "Notification sent to Discord"}
    except Exception as e:
        log.error("Failed to send Discord notification: %s", e)
        return {"status": "error", "message": str(e)}


@router.post("/discord/test")
async def send_discord_test():
    """
    Send a single test message to Discord using current config.
    Used by the setup step "Send test notification" button; backend owns all Discord sends.
    """
    webhook_url = discord_config.get_webhook_url()
    if not webhook_url:
        return {"status": "disabled", "message": "Discord notifications not configured"}
    try:
        notify_discord(webhook_url, "ℹ️ Test notification from MKV-Auto")
        return {"status": "sent", "message": "Test notification sent to Discord"}
    except Exception as e:
        log.error("Failed to send Discord test notification: %s", e)
        return {"status": "error", "message": str(e)}


@router.get("/devmode")
async def devmode_status():
    """
    Simple dev-mode status endpoint so the UI can show the DEV badge even without active jobs.
    """
    return {
        "enabled": is_dev_mode(),
        "repo_url": get_discdb_repo_url(),
        "branch": get_discdb_repo_branch(),
        "repo_path": str(get_discdb_repo_path()),
        "export_root": str(get_export_root()),
    }


class QuickPostprocessTestsUpdate(BaseModel):
    enabled: bool


@router.get("/quick-postprocess-tests")
async def get_quick_postprocess_tests() -> dict:
    """Get quick post-process tests toggle (dev only; used when ENABLE_DEVMODE=1)."""


@router.post("/quick-postprocess-tests")
async def set_quick_postprocess_tests(body: QuickPostprocessTestsUpdate) -> dict:
    """Set quick post-process tests toggle (dev only)."""


class FfmpegDetectionUpdate(BaseModel):
    enabled: bool


class DiscdbDisabledUpdate(BaseModel):
    """Request body for the 'Disable DiscDB' dev toggle."""
    disabled: bool


@router.get("/discdb-disabled")
async def get_discdb_disabled() -> dict:
    """Get the 'Disable DiscDB' dev toggle (skip TheDiscDB lookups when on).
    Default OFF — production behaviour. Only meaningful when ENABLE_DEVMODE=1.
    """


@router.post("/discdb-disabled")
async def set_discdb_disabled(body: DiscdbDisabledUpdate) -> dict:
    """Set the 'Disable DiscDB' dev toggle. When True, the backend
    short-circuits TheDiscDB lookups so every disc lands in the miss
    branch; the frontend forces the miss workflow regardless of cached
    discdb_result.
    """


@router.get("/ffmpeg-detection")
async def get_ffmpeg_detection() -> dict:
    """Get FFmpeg detection toggle (dev only; used when ENABLE_DEVMODE=1)."""


@router.post("/ffmpeg-detection")
async def set_ffmpeg_detection(body: FfmpegDetectionUpdate) -> dict:
    """Set FFmpeg detection toggle (dev only)."""


@router.post("/transfer/rsync/validate")
async def validate_rsync_config(req: RsyncConfig | None = None):
    cfg = req
    saved, has_key = rsync_transfer.load_config()
    if cfg is None:
        cfg = saved
    if not cfg:
        raise HTTPException(400, detail="No rsync configuration provided or saved")
    if not has_key and not rsync_transfer.has_key():
        raise HTTPException(400, detail="SSH key not uploaded")
    ok, msg = rsync_transfer.validate_connection(cfg)
    if not ok:
        raise HTTPException(400, detail=msg)
    return {"status": "ok", "message": msg}


@router.post("/export")
async def export_rip_history(db: Session = Depends(get_db)) -> FileResponse:
    """
    Export rip history to a ZIP file containing database records and job directories
    (excluding MKV files and preview data).
    """
    try:
        tmp_dir = get_mkvauto_root() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        zip_filename = f"mkv-auto-export-{timestamp}.zip"
        zip_path = tmp_dir / zip_filename
        
        # Create export ZIP
        export_import.create_export_zip(db, zip_path)
        
        return FileResponse(
            zip_path,
            filename=zip_filename,
            media_type="application/zip",
            headers={"Content-Disposition": f"attachment; filename={zip_filename}"}
        )
    except Exception as exc:
        log.exception("Failed to export rip history")
        raise HTTPException(status_code=500, detail=f"Export failed: {str(exc)}") from exc


@router.post("/import", response_model=ImportSummary)
async def import_rip_history(
    file: UploadFile,
    db: Session = Depends(get_db)
) -> ImportSummary:
    """
    Import rip history from a ZIP file.
    Merges with existing data (skips duplicates).
    """
    if not file.filename or not file.filename.endswith('.zip'):
        raise HTTPException(400, detail="File must be a ZIP archive")
    
    # Validate file size (500MB max)
    file_content = await file.read()
    if len(file_content) > export_import.MAX_ZIP_SIZE:
        raise HTTPException(400, detail=f"File exceeds maximum size of {export_import.MAX_ZIP_SIZE / (1024*1024):.0f}MB")
    
    temp_dir = None
    try:
        # Create temporary directory for extraction
        temp_dir = Path(tempfile.mkdtemp(prefix="mkv_import_"))
        
        # Save uploaded file
        zip_path = temp_dir / file.filename
        with open(zip_path, 'wb') as f:
            f.write(file_content)
        
        # Extract ZIP
        import zipfile
        with zipfile.ZipFile(zip_path, 'r') as zipf:
            # Validate structure
            if 'database.json' not in zipf.namelist():
                raise HTTPException(400, detail="ZIP archive missing database.json")
            
            # Sanitize paths and extract
            for member in zipf.namelist():
                # Prevent directory traversal
                if '..' in member or member.startswith('/'):
                    log.warning(f"Skipping potentially unsafe path in ZIP: {member}")
                    continue
                
                zipf.extract(member, temp_dir)
        
        # Load database.json
        db_json_path = temp_dir / "database.json"
        with open(db_json_path, 'r') as f:
            export_data = json.load(f)
        
        if "database" not in export_data:
            raise HTTPException(400, detail="Invalid export format: missing 'database' key")
        
        # Import database records
        summary_dict = export_import.deserialize_database(export_data, db)
        
        # Extract job directories
        jobs_dir = temp_dir / "jobs"
        if jobs_dir.exists():
            jobs_root = get_mkvauto_data() / "jobs"
            jobs_root.mkdir(parents=True, exist_ok=True)
            
            # Copy job directories
            for job_dir in jobs_dir.iterdir():
                if job_dir.is_dir():
                    target_dir = jobs_root / job_dir.name
                    if target_dir.exists():
                        # Merge: copy new files, don't overwrite existing
                        shutil.copytree(job_dir, target_dir, dirs_exist_ok=True)
                    else:
                        shutil.copytree(job_dir, target_dir)
        
        return ImportSummary(**summary_dict)
        
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("Failed to import rip history")
        raise HTTPException(status_code=500, detail=f"Import failed: {str(exc)}") from exc
    finally:
        # Cleanup temp directory
        if temp_dir and temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)


class FrontendLogRequest(BaseModel):
    """Request model for frontend log endpoint."""
    level: str
    facility: str
    message: str
    timestamp: Optional[int] = None


@router.post("/log")
async def frontend_log(req: FrontendLogRequest):
    """
    Receive frontend logs and write them to frontend.log file.
    """
    try:
        logs_dir = get_mkvauto_root() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)

        # Use RotatingFileHandler for frontend.log
        frontend_log_path = logs_dir / "frontend.log"

        # Create a logger specifically for frontend logs
        frontend_logger = logging.getLogger("frontend")
        frontend_logger.setLevel(logging.DEBUG)

        # Check if handler already exists
        if not any(isinstance(h, RotatingFileHandler) and h.baseFilename == str(frontend_log_path) for h in frontend_logger.handlers):
            handler = RotatingFileHandler(
                frontend_log_path, maxBytes=LOG_ROTATE_MAX_BYTES, backupCount=LOG_ROTATE_BACKUP_COUNT
            )
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
            handler.setFormatter(formatter)
            handler.setLevel(logging.DEBUG)
            frontend_logger.addHandler(handler)
        
        # Map frontend log levels to Python logging levels
        level_map = {
            "ERROR": logging.ERROR,
            "WARNING": logging.WARNING,
            "INFO": logging.INFO,
            "DEBUG": logging.DEBUG,
        }
        
        log_level = level_map.get(req.level.upper(), logging.INFO)
        log_message = f"{req.facility} {req.message}"
        
        # Log the message
        frontend_logger.log(log_level, log_message)
        
        return {"status": "ok"}
    except Exception as exc:
        log.warning("Failed to write frontend log: %s", exc)
        return {"status": "error", "message": str(exc)}
