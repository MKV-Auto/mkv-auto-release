"""
WebSocket endpoints for real-time workflow context updates.

Provides:
- /ws/workflow - Unified endpoint for all real-time workflow updates (replaces coordinator, per-disc, and per-job endpoints)
"""
import asyncio
import logging
import json
import os
import time
from datetime import datetime
from typing import Dict, Any, Optional, List
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException, Depends
from sqlalchemy.orm import Session, joinedload

from core.websocket_manager import get_websocket_manager
from core.disc_manager import get_cached_discs
from api import database, crud
from api import models as db_models
from api import schemas
from api.routers.discs import get_disc_workflow_context_by_id
from api.routers.jobs import get_job_workflow_context, get_unfinished_jobs_workflow_contexts

router = APIRouter(prefix="/ws", tags=["websockets"])
# Prefix /coordinator so that behind nginx (which strips /api) the path is /coordinator/initial-state
http_router = APIRouter(prefix="/coordinator", tags=["coordinator"])
logger = logging.getLogger("api.routers.websockets")

# Keepalive configuration
WEBSOCKET_PING_INTERVAL = float(os.getenv("WEBSOCKET_PING_INTERVAL", "30"))  # seconds
WEBSOCKET_PONG_TIMEOUT = float(os.getenv("WEBSOCKET_PONG_TIMEOUT", "90"))  # seconds


def get_db():
    """Database dependency for websocket handlers."""
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _resolve_in_drive_job_for_disc(
    db: Session, disc_id: str
) -> tuple[Optional[str], Optional[str], Optional[datetime]]:
    """
    Job to attach to an in-drive carousel card:
      1. Prefer pending/running/validating (a live rip is in flight).
      2. If any completed job exists for this disc, return no job — the
         "Already in Library" signal (derived downstream from
         has_completed_job in _build_disc_metadata) takes precedence over
         resurfacing an older failed attempt. Without this, a disc that
         had a failed rip followed by a successful re-rip kept rendering
         the "FAILED DISC" card because the failed row was still the
         newest terminal match. Mirrors the "completed supersedes failed"
         filter in unfinished_jobs.query_unfinished_jobs (:117-138).
      3. Fall back to latest failed (same intent as unfinished summaries).
    Returns (job_id, job_status, job_created_at).
    """
    active_job = (
        db.query(db_models.Job)
        .filter(
            db_models.Job.disc_id == disc_id,
            db_models.Job.job_status.in_(["pending", "running", "validating"]),
        )
        .order_by(db_models.Job.created_at.desc())
        .first()
    )
    if active_job:
        return (str(active_job.id), active_job.job_status, active_job.created_at)
    has_completed = (
        db.query(db_models.Job.id)
        .filter(
            db_models.Job.disc_id == disc_id,
            db_models.Job.job_status == "completed",
        )
        .first()
    )
    if has_completed:
        return (None, None, None)
    failed_job = (
        db.query(db_models.Job)
        .filter(
            db_models.Job.disc_id == disc_id,
            db_models.Job.job_status == "failed",
        )
        .order_by(db_models.Job.created_at.desc())
        .first()
    )
    if failed_job:
        return (str(failed_job.id), "failed", failed_job.created_at)
    return (None, None, None)


def _resolve_live_mount_for_job(job: db_models.Job) -> Optional[str]:
    """#576: return the live ``/dev/srN`` for the drive that ran this job,
    looked up by ``Job.drive_by_id_serial`` (the stable hardware identity
    persisted in #549/#556).

    USB optical drives renumber across hot-plug. ``Job.mount_point`` is
    set at job-create time and never updated, so the failed-job card's
    displayed device path can drift away from reality. The carousel
    consumes this value to show "/dev/srN" on the failed-disc card —
    surfacing the LIVE mount instead of the persisted one keeps the UI
    self-consistent without a frontend hot-stream merge.

    Returns the resolved mount when:
      - the job has a non-empty ``drive_by_id_serial``, AND
      - the drive identified by that serial is currently attached.

    Falls back to ``job.mount_point`` when either condition fails — that
    preserves today's behaviour for ATAPI/SATA drives (no by-id serial)
    and for cases where the original drive has been physically removed.
    """
    serial = (getattr(job, "drive_by_id_serial", None) or "").strip()
    if serial:
        try:
            from core.drive_identity import resolve_current_mount_point_for_serial
            live = resolve_current_mount_point_for_serial(serial)
        except Exception:
            live = None
        if live:
            return live
    persisted = getattr(job, "mount_point", None)
    return persisted


def _build_disc_metadata(disc: db_models.Disc, disc_state: str, job_id: Optional[str] = None, 
                         disc_num: Optional[str] = None, mount_point: Optional[str] = None,
                         created_at: Optional[datetime] = None, 
                         scan_state: Optional[str] = None, scan_error: Optional[str] = None,
                         db: Optional[Any] = None,
                         job_status: Optional[str] = None) -> schemas.DiscMetadata:
    """
    Build DiscMetadata from a disc record for coordinator messages.
    
    Args:
        disc: Disc database record
        disc_state: 'in_drive' or 'unfinished'
        job_id: Optional job_id for unfinished discs
        disc_num: Optional disc_num (for inserted discs)
        mount_point: Optional mount_point (for inserted discs)
        created_at: Optional job creation time (for unfinished discs - rip creation time)
        scan_state: Optional scan state ('pending', 'scanning', 'ready', 'failed')
        scan_error: Optional error message if scan failed
        db: Optional database session for has_completed_job query
        job_status: Optional job row status when job_id refers to a Job (e.g. failed)
    """
    # Get movie/release metadata
    movie_name = None
    release_name = None
    release_image = None
    release_year = None
    production_year = None

    if disc.release:
        rel = disc.release
        release_name = rel.name
        release_year = rel.release_year
        if rel.movie:
            movie_name = rel.movie.name
            production_year = rel.movie.production_year
        # Get release image (title_cover_url removed from Release - should be on Disc)
        release_image = rel.cover_front_url
    
    # Get resolution from release or disc
    resolution = None
    if disc.release:
        resolution = disc.release.resolution
    
    # Determine scan_state if not provided
    # If disc has content_hash and scan_state is None, it's ready
    # If disc has no content_hash and is in_drive, it's pending or scanning
    if scan_state is None:
        if disc.content_hash:
            scan_state = 'ready'
        elif disc_state == 'in_drive':
            # Check disc.scan_state from database if available
            scan_state = getattr(disc, 'scan_state', None) or 'pending'
        else:
            scan_state = None
    
    # Check for any completed job for this disc (for green check badge on in-drive cards)
    has_completed_job: Optional[bool] = None
    if disc_state == 'in_drive' and db is not None:
        try:
            has_completed_job = db.query(
                db.query(db_models.Job).filter(
                    db_models.Job.disc_id == disc.id,
                    db_models.Job.job_status == "completed",
                ).exists()
            ).scalar()
        except Exception:
            has_completed_job = None

    # #603: surface the "already in Library" signal so the carousel can
    # collapse the usual "Now Reading" treatment into a single
    # "Already in Library" card with a Re-rip button. Fires when the
    # disc has either (a) been finalized in the Library OR (b) has a
    # completed job attached — both indicate the user has already taken
    # this disc through to completion at least once, and a re-rip is
    # the deliberate action. A linked release is required so the card
    # has a meaningful name to show.
    finalized_flag: Optional[bool] = None
    finalized_release_id: Optional[str] = None
    finalized_release_name: Optional[str] = None
    finalized_release_slug: Optional[str] = None
    if disc.release is not None and (getattr(disc, "finalized", False) or bool(has_completed_job)):
        finalized_flag = True
        finalized_release_id = disc.release_id
        finalized_release_slug = disc.release.slug
        # Prefer the movie's name (matches the Library card title); fall
        # back to the release edition string when the join is incomplete.
        rel_movie = getattr(disc.release, "movie", None)
        finalized_release_name = (
            (rel_movie.name if rel_movie else None)
            or disc.release.name
        )

    return schemas.DiscMetadata(
        disc_id=str(disc.id),
        disc_num=disc_num,
        mount_point=mount_point,
        disc_hash=disc.content_hash,
        disc_state=disc_state,
        job_id=job_id,
        scan_state=scan_state,
        scan_error=scan_error,
        movie_name=movie_name,
        release_name=release_name,
        info_title=disc.info_title,
        disc_number=disc.disc_number,
        discdb_disc_num=getattr(disc, "discdb_disc_num", None),
        release_image=release_image,
        disc_format=disc.format,
        resolution=resolution,
        release_year=release_year,
        production_year=production_year,
        last_modified_at=disc.updated_at,
        created_at=created_at,
        has_completed_job=has_completed_job,
        job_status=job_status,
        finalized=finalized_flag,
        finalized_release_id=finalized_release_id,
        finalized_release_name=finalized_release_name,
        finalized_release_slug=finalized_release_slug,
    )


def _build_initial_coordinator_state_sync() -> Dict[str, Any]:
    """
    Build initial coordinator state in a sync context. No DB session is held across await.
    Call from async via run_in_executor.
    """
    cached_discs = get_cached_discs()
    db = database.SessionLocal()
    try:
        inserted_discs_metadata = []
        for disc_info in cached_discs:
            disc_id = disc_info.get("disc_id")
            disc_num = disc_info.get("disc_num")
            mount_point = disc_info.get("mount_point")

            if disc_id:
                disc = (
                    db.query(db_models.Disc)
                    .options(
                        joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
                    )
                    .filter(db_models.Disc.id == disc_id)
                    .first()
                )
                if disc:
                    jid, jstat, jcreated = _resolve_in_drive_job_for_disc(db, disc_id)
                    metadata = _build_disc_metadata(
                        disc,
                        disc_state='in_drive',
                        job_id=jid,
                        job_status=jstat,
                        created_at=jcreated,
                        disc_num=disc_num,
                        mount_point=mount_point,
                        db=db,
                    )
                    inserted_discs_metadata.append(metadata.model_dump(mode='json'))
            elif disc_num and mount_point:
                inserted_discs_metadata.append({
                    "disc_id": f"pending-{disc_num}",
                    "disc_num": disc_num,
                    "mount_point": mount_point,
                    "disc_hash": None,
                    "disc_state": "in_drive",
                    "job_id": None,
                    "scan_state": "pending",
                    "scan_error": None,
                    "movie_name": None,
                    "release_name": None,
                    "info_title": None,
                    "disc_number": None,
                    "discdb_disc_num": None,
                    "release_image": None,
                    "disc_format": None,
                    "resolution": None,
                    "release_year": None,
                    "production_year": None,
                    "last_modified_at": None,
                })

        # Include drives that are currently being scanned but not yet in cache.
        # This ensures the frontend shows a "scanning" card after page refresh.
        from core.disc_slot_state import get_scanning_mount_points
        import re as _re

        scanning_mounts = get_scanning_mount_points()
        represented_mounts = {
            m.get("mount_point") for m in inserted_discs_metadata if m.get("mount_point")
        }
        for mp in scanning_mounts:
            if mp not in represented_mounts:
                # Derive disc_num from /dev/srN for display (not used as key)
                dn = None
                sr_match = _re.search(r"sr(\d+)$", mp)
                if sr_match:
                    dn = sr_match.group(1)
                inserted_discs_metadata.append({
                    "disc_id": f"scanning-{dn or mp}",
                    "disc_num": dn,
                    "mount_point": mp,
                    "disc_hash": None,
                    "disc_state": "in_drive",
                    "job_id": None,
                    "scan_state": "scanning",
                    "scan_error": None,
                    "movie_name": None,
                    "release_name": None,
                    "info_title": None,
                    "disc_number": None,
                    "discdb_disc_num": None,
                    "release_image": None,
                    "disc_format": None,
                    "resolution": None,
                    "release_year": None,
                    "production_year": None,
                    "last_modified_at": None,
                })

        # Active-rip fallback: if a job is running (pending/running/validating)
        # on a mount_point that isn't represented above, emit an in_drive card
        # for it. This handles the case where the drive-manager cache is empty
        # for a mount but the disc is physically loaded and being ripped —
        # most commonly right after a uvicorn restart, where the startup
        # insert-scan defers `info dev:` (rip in progress → MSG:5010 conflict)
        # and thus doesn't populate disc_cache. Without this the disc silently
        # disappears from the carousel until the rip finishes.
        represented_mounts_after_scanning = {
            m.get("mount_point")
            for m in inserted_discs_metadata
            if m.get("mount_point")
        }
        active_rip_jobs = (
            db.query(db_models.Job)
            .options(
                joinedload(db_models.Job.disc)
                    .joinedload(db_models.Disc.release)
                    .joinedload(db_models.Release.movie),
            )
            .filter(
                db_models.Job.job_status.in_(("pending", "running", "validating")),
                db_models.Job.mount_point.isnot(None),
            )
            .all()
        )
        for job in active_rip_jobs:
            mp = getattr(job, "mount_point", None)
            if not mp or mp in represented_mounts_after_scanning:
                continue
            disc = getattr(job, "disc", None)
            if disc is None:
                continue
            metadata = _build_disc_metadata(
                disc,
                disc_state='in_drive',
                job_id=str(job.id),
                job_status=job.job_status,
                created_at=job.created_at,
                disc_num=getattr(job, "disc_num", None),
                mount_point=mp,
                scan_state='ready',
                db=db,
            )
            inserted_discs_metadata.append(metadata.model_dump(mode='json'))
            represented_mounts_after_scanning.add(mp)

        inserted_disc_ids = {
            m["disc_id"] for m in inserted_discs_metadata
            if not str(m.get("disc_id", "")).startswith(("pending-", "scanning-"))
        }

        # #498 — share the unfinished-job query with /jobs/unfinished/summaries
        # so the WS snapshot and HTTP carousel load see the same set
        # (previously this filter dropped `pending` and `failed` cards, so
        # they vanished on Ripper-page re-entry).
        from api.unfinished_jobs import query_unfinished_jobs
        unfinished_jobs = query_unfinished_jobs(db)

        unfinished_discs_metadata = []
        unfinished_jobs_list = []
        for job in unfinished_jobs:
            if job.disc:
                disc = job.disc
                disc_id_str = str(disc.id)
                if disc_id_str in inserted_disc_ids:
                    continue
                metadata = _build_disc_metadata(
                    disc,
                    disc_state='unfinished',
                    job_id=str(job.id),
                    created_at=job.created_at,
                    job_status=job.job_status,
                )
                unfinished_discs_metadata.append(metadata.model_dump(mode='json'))
                unfinished_jobs_list.append({
                    "job_id": str(job.id),
                    "disc_id": disc_id_str,
                    # #576: Job.mount_point is recorded at job-create time
                    # and never updated — when the drive renumbers across
                    # hot-plug, the value goes stale and the failed-job
                    # card shows the wrong device path. Resolve the live
                    # mount_point from Job.drive_by_id_serial via the
                    # #549 helper; fall back to the persisted value when
                    # the drive isn't currently attached.
                    "mount_point": _resolve_live_mount_for_job(job),
                })

        all_discs = inserted_discs_metadata + unfinished_discs_metadata
        return {
            "type": "initial_state",
            "discs": all_discs,
            "unfinished_jobs": unfinished_jobs_list,
        }
    finally:
        db.close()


async def _get_initial_coordinator_state() -> Dict[str, Any]:
    """
    Get initial state for master coordinator websocket.
    Runs DB work in executor so no session is held across await.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _build_initial_coordinator_state_sync)


@http_router.get("/initial-state")
async def get_initial_state_http():
    """
    HTTP endpoint to get initial coordinator state (fallback when WebSocket fails).
    
    Returns the same data as the WebSocket initial_state message.
    """
    state = await _get_initial_coordinator_state()
    return state


async def _send_ping_keepalive(websocket: WebSocket, stop_event: asyncio.Event, connection_type: str = "websocket") -> None:
    """
    Background task to send periodic ping messages to keep connection alive.
    
    Args:
        websocket: WebSocket connection
        stop_event: Event to signal when to stop sending pings
        connection_type: Type of connection for logging (default: "websocket")
    """
    try:
        while not stop_event.is_set():
            await asyncio.sleep(WEBSOCKET_PING_INTERVAL)
            if stop_event.is_set():
                break
            try:
                await websocket.send_json({"type": "ping"})
                logger.debug(f"Sent ping to {connection_type} websocket")
            except (WebSocketDisconnect, Exception) as exc:
                logger.debug(f"Error sending ping to {connection_type} websocket: {exc}")
                break
    except asyncio.CancelledError:
        logger.debug("Ping keepalive task cancelled")
    except Exception as exc:
        logger.error(f"Error in ping keepalive task: {exc}", exc_info=True)


@router.websocket("/workflow")
async def workflow_unified(websocket: WebSocket):
    """
    Unified websocket endpoint for all real-time workflow updates.
    
    Replaces the previous separate endpoints:
    - /ws/workflow-coordinator
    - /ws/workflow/{disc_id}
    - /ws/workflow/job/{job_id}
    
    Emits:
    - disc_inserted: When a disc is inserted
    - disc_ejected: When a disc is ejected
    - disc_updated: When disc metadata changes
    - job_unfinished: When a job becomes unfinished
    - job_finished: When a job finishes
    - progress_update: When job progress changes (includes job_id, disc_id, per_title_progress, etc.)
    - context_changed: When workflow context changes (includes disc_id or job_id, changed_fields)
    - makemkv_update_log: MakeMKV installer log line (includes job_id, line)
    - makemkv_update_status: MakeMKV installer status change (includes job_id, status, error, version)
    - ping: Periodic keepalive message (every 30 seconds)
    
    Receives:
    - ping: Client ping (responds with pong)
    - pong: Response to server ping
    - request_sync: Request full current state (for backward compatibility, but initial state should come from HTTP)
    - request_progress: Request current progress for a job (includes job_id in payload)
    
    Note: Initial state should be fetched via HTTP endpoint /api/coordinator/initial-state on page load.
    All messages include context identifiers (disc_id, job_id) for proper routing on the frontend.
    """
    await websocket.accept()
    manager = get_websocket_manager()
    key = "unified"
    
    # Register connection
    if not await manager.connect(key, websocket):
        await websocket.close(code=1008, reason="Connection limit exceeded")
        return
    
    # Start keepalive task
    stop_ping_event = asyncio.Event()
    ping_task = asyncio.create_task(_send_ping_keepalive(websocket, stop_ping_event, "unified workflow"))
    
    try:
        # Listen for messages
        while True:
            try:
                data = await websocket.receive_json()
                message_type = data.get("type")
                
                if message_type == "request_sync":
                    # Send full current state
                    state = await _get_initial_coordinator_state()
                    await websocket.send_json(state)
                    logger.info("Sent sync state to unified workflow websocket")
                elif message_type == "request_progress":
                    # Request progress for a specific job
                    job_id = data.get("job_id")
                    if job_id:
                        db = database.SessionLocal()
                        try:
                            job = crud.get_job(db, job_id)
                            if job:
                                # Also get disc_id if available
                                disc_id = str(job.disc_id) if job.disc_id else None
                                await websocket.send_json({
                                    "type": "progress_update",
                                    "job_id": job_id,
                                    "disc_id": disc_id,
                                    "rip_progress": job.rip_progress,
                                    "rip_phase": getattr(job, "rip_phase", None),
                                    "post_progress": getattr(job, "post_progress", 0),
                                    "transfer_progress": getattr(job, "transfer_progress", None),
                                    "per_title_progress": getattr(job, "per_title_progress", None),
                                    "current_title_progress": getattr(job, "current_title_progress", None),
                                    "current_title_id": getattr(job, "current_title_id", None),
                                    "current_title_number": getattr(job, "current_title_number", None),
                                })
                                logger.info(f"Sent progress to unified workflow websocket for job: {job_id}")
                        finally:
                            db.close()
                    else:
                        logger.warning("request_progress message missing job_id")
                elif message_type == "pong":
                    # Client responded to ping - connection is alive
                    logger.debug("Received pong from unified workflow websocket")
                elif message_type == "ping":
                    # Client sent ping - respond with pong
                    await websocket.send_json({"type": "pong"})
                    logger.debug("Responded to ping from unified workflow websocket")
                else:
                    logger.warning(f"Unknown message type from unified workflow websocket: {message_type}")
                    
            except WebSocketDisconnect:
                break
            except json.JSONDecodeError:
                logger.warning("Invalid JSON received from unified workflow websocket")
            except RuntimeError as exc:
                # Check if the error indicates the websocket is not connected
                if "WebSocket is not connected" in str(exc):
                    logger.warning(f"WebSocket connection lost for unified workflow websocket: {exc}")
                    break
                raise  # Re-raise if it's a different RuntimeError
            except Exception as exc:
                logger.error(f"Error handling message from unified workflow websocket: {exc}", exc_info=True)
                
    except WebSocketDisconnect:
        logger.info("Unified workflow websocket disconnected")
    except Exception as exc:
        logger.error(f"Error in unified workflow websocket: {exc}", exc_info=True)
    finally:
        # Stop keepalive task
        stop_ping_event.set()
        ping_task.cancel()
        try:
            await ping_task
        except asyncio.CancelledError:
            pass
        await manager.disconnect(key, websocket)


async def _emit_unified(message: Dict[str, Any]) -> None:
    """
    Emit a message to all unified workflow websocket connections.
    
    Args:
        message: Message dict with type and context identifiers (disc_id, job_id, etc.)
    """
    manager = get_websocket_manager()
    sent_count = await manager.broadcast("unified", message)
    if sent_count > 0:
        logger.debug(f"Emitted {message.get('type')} to {sent_count} unified workflow connections")
    return sent_count


async def _emit_to_coordinator(event_type: str, payload: Dict[str, Any]) -> None:
    """
    Emit an event to all unified workflow websocket connections.
    
    Args:
        event_type: Event type (disc_inserted, disc_ejected, job_unfinished, job_finished, disc_updated)
        payload: Event payload (should include disc_id, job_id, etc. for routing)
    """
    message = {
        "type": event_type,
        **payload,
    }
    sent_count = await _emit_unified(message)
    if sent_count > 0:
        logger.info(f"Emitted {event_type} to {sent_count} unified workflow connections")


def _build_disc_updated_payload_with_job(disc_id: str, job_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """Sync: build disc_updated payload for coordinator. No session held across await. Returns None if disc not found."""
    db = database.SessionLocal()
    try:
        disc = (
            db.query(db_models.Disc)
            .options(
                joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
            )
            .filter(db_models.Disc.id == disc_id)
            .first()
        )
        if not disc:
            return None
        resolved_id = job_id
        job_status_kw: Optional[str] = None
        created_kw: Optional[datetime] = None
        if resolved_id is not None:
            row = db.query(db_models.Job).filter(db_models.Job.id == resolved_id).first()
            if row:
                job_status_kw = row.job_status
                created_kw = row.created_at
        else:
            resolved_id, job_status_kw, created_kw = _resolve_in_drive_job_for_disc(db, disc_id)
        cached_discs = get_cached_discs()
        disc_num = None
        mount_point = None
        for cached_disc in cached_discs:
            if cached_disc.get("disc_id") == disc_id:
                disc_num = cached_disc.get("disc_num")
                mount_point = cached_disc.get("mount_point")
                break
        metadata = _build_disc_metadata(
            disc,
            disc_state='in_drive',
            job_id=resolved_id,
            job_status=job_status_kw,
            created_at=created_kw,
            disc_num=disc_num,
            mount_point=mount_point,
            scan_state='ready',
            db=db,
        )
        return metadata.model_dump(mode='json')
    finally:
        db.close()


async def _emit_disc_updated_with_job(disc_id: str, job_id: Optional[str] = None) -> None:
    """
    Emit disc_updated message to coordinator when job is created or updated for a disc.
    DB work runs in executor so no session is held across await.
    """
    try:
        loop = asyncio.get_running_loop()
        payload = await loop.run_in_executor(None, lambda: _build_disc_updated_payload_with_job(disc_id, job_id))
        if payload is None:
            logger.warning(f"Disc {disc_id} not found when emitting disc_updated with job")
            return
        await _emit_to_coordinator("disc_updated", payload)
        logger.info(f"Emitted disc_updated for disc {disc_id} with job_id {payload.get('job_id')} (scan_state: ready)")
    except Exception as exc:
        logger.warning(f"Failed to emit disc_updated with job for disc {disc_id}: {exc}")


def _build_disc_updated_payload_from_info(
    disc_id: str, disc_num: str, mount_point: str, scan_state: str
) -> Optional[Dict[str, Any]]:
    """Sync: build disc_updated payload from disc record. No session held across await. Returns None if disc not found."""
    db = database.SessionLocal()
    try:
        disc = (
            db.query(db_models.Disc)
            .options(
                joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
            )
            .filter(db_models.Disc.id == disc_id)
            .first()
        )
        if not disc:
            return None
        jid, jstat, jcreated = _resolve_in_drive_job_for_disc(db, disc_id)
        metadata = _build_disc_metadata(
            disc,
            disc_state='in_drive',
            job_id=jid,
            job_status=jstat,
            created_at=jcreated,
            disc_num=disc_num,
            mount_point=mount_point,
            scan_state=scan_state,
            db=db,
        )
        return metadata.model_dump(mode='json')
    finally:
        db.close()


async def _emit_disc_updated_from_info(disc_info: dict, disc_num: str, mount_point: str, scan_state: str = 'ready') -> None:
    """
    Emit disc_updated message to coordinator when disc metadata becomes available.
    DB work runs in sync; only emit is awaited so no session is held across await.
    """
    disc_id = disc_info.get("disc_id")
    if not disc_id:
        return
    try:
        payload = _build_disc_updated_payload_from_info(disc_id, disc_num, mount_point, scan_state)
        if payload is None:
            return
        await _emit_to_coordinator("disc_updated", payload)
        logger.info(f"Emitted disc_updated for disc {disc_id} (disc_num: {disc_num}, scan_state: {scan_state})")
    except Exception as exc:
        logger.warning(f"Failed to emit disc_updated for disc {disc_id}: {exc}")


async def _emit_to_disc_workflow(disc_id: str, changed_fields: Optional[List[str]] = None) -> None:
    """
    Emit context change notification to all unified workflow connections.
    
    Args:
        disc_id: Disc ID
        changed_fields: List of fields that changed (e.g., ['labelForm', 'jobStatus'])
    """
    message = {
        "type": "context_changed",
        "disc_id": disc_id,
        "changed_fields": changed_fields or ["context"]
    }
    sent_count = await _emit_unified(message)
    if sent_count > 0:
        logger.info(f"Emitted context change notification to {sent_count} unified workflow connections for disc: {disc_id}")


async def _emit_to_job_workflow(job_id: str, changed_fields: Optional[List[str]] = None) -> None:
    """
    Emit context change notification to all unified workflow connections.
    Uses in-memory job_id->disc_id cache to avoid DB lookups.
    
    Args:
        job_id: Job ID
        changed_fields: List of fields that changed
    """
    disc_id = await _get_disc_id_for_job(job_id)
    
    message = {
        "type": "context_changed",
        "job_id": job_id,
        "changed_fields": changed_fields or ["context"]
    }
    if disc_id:
        message["disc_id"] = disc_id
    
    sent_count = await _emit_unified(message)
    if sent_count > 0:
        logger.info(f"Emitted context change notification to {sent_count} unified workflow connections for job: {job_id}")


# In-memory cache: job_id -> disc_id (avoids DB lookup on every progress update)
_job_disc_id_cache: Dict[str, str] = {}


def _cache_job_disc_id(job_id: str, disc_id: str) -> None:
    """Cache a job_id -> disc_id mapping (called when job is created or first queried)."""
    _job_disc_id_cache[job_id] = disc_id


def _clear_job_disc_id_cache(job_id: str) -> None:
    """Clear cached disc_id for a job (called when job is deleted)."""
    _job_disc_id_cache.pop(job_id, None)


async def _get_disc_id_for_job(job_id: str, progress_data: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """
    Get disc_id for a job, using in-memory cache to avoid DB lookups.
    Falls back to DB lookup only on cache miss.
    """
    # 1. Check if disc_id is in the progress data (worker may include it)
    if progress_data and progress_data.get("disc_id"):
        disc_id = str(progress_data["disc_id"])
        _job_disc_id_cache[job_id] = disc_id
        return disc_id
    
    # 2. Check in-memory cache
    if job_id in _job_disc_id_cache:
        return _job_disc_id_cache[job_id]
    
    # 3. Cache miss: DB lookup (only happens once per job)
    try:
        db = database.SessionLocal()
        try:
            job = crud.get_job(db, job_id)
            if job and job.disc_id:
                disc_id = str(job.disc_id)
                _job_disc_id_cache[job_id] = disc_id
                return disc_id
        finally:
            db.close()
    except Exception as exc:
        logger.warning(f"Failed to get disc_id for job {job_id}: {exc}")
    
    return None


async def _emit_job_progress(job_id: str, progress_data: Dict[str, Any]) -> None:
    """
    Emit job progress update to all unified workflow connections.
    
    Uses in-memory job_id->disc_id cache to avoid DB lookups on every progress update.
    The cache is populated from:
    1. disc_id included in the progress data by the worker
    2. Previous lookups cached in _job_disc_id_cache
    3. One-time DB fallback on cache miss
    
    Args:
        job_id: Job ID
        progress_data: Progress data dict (rip_progress, post_progress, per_title_progress, etc.)
    """
    disc_id = await _get_disc_id_for_job(job_id, progress_data)
    
    message = {
        "type": "progress_update",
        "job_id": job_id,
        **progress_data,
    }
    if disc_id:
        message["disc_id"] = disc_id
    
    sent_count = await _emit_unified(message)
    if sent_count > 0:
        logger.debug(f"Emitted progress update to {sent_count} unified workflow connections for job: {job_id}")

