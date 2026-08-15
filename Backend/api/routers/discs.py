import logging, json, asyncio, time, os
from typing import Any, Dict, List, Optional
from fastapi import APIRouter, HTTPException, Depends
from sqlalchemy.orm import Session
from pydantic import ValidationError

from core.disc_cache import set as set_cached, get as get_cached
from core.disc_manager import get_disc_info, refresh_disc_info, list_discs, get_cached_discs
from core.disc_cache import get as cache_get
from core.utils import get_disc_size_bytes_for_mount_point
from core.title_type_normalize import normalize_title_type_for_api as _normalize_title_type
from core.duplicate_info import attach_duplicate_info
from core.job_paths import JobPaths
from core.ffprobe_metadata import metadata_scan_to_summary
from core.drive_manager_client import DriveManagerError
from core.redis_cache import get as redis_get, set as redis_set, invalidate as redis_invalidate, get_or_fetch as redis_get_or_fetch
from api import database, crud
from api.schemas import (
    DiscJobState,
    JobStatus,
    DiscDetail,
    WorkflowContextResponse,
    WorkflowContextUpdate,
    TitlePatchRequest,
    TitlePatchBatchRequest,
    TitlePatchResponse,
    TitlePatchBatchResponse,
    TitlePatchResult,
    MovieSummary,
    BoxsetSummary,
    ReleaseSummary,
    MovieCreate,
    BoxsetCreate,
    SegmentFlagPatchRequest,
    SegmentFlagPatchResponse,
    RemainingPlaylistSizeResponse,
)
from api import models as db_models
from sqlalchemy.orm import joinedload, selectinload
from fastapi import Query, Body
from typing import Set
from api import scan_guard
from parsing.disc_parser import hydrate_disc_payload

router = APIRouter(prefix="/discs", tags=["discs"])

# Import job-specific functions for unified contexts
def _get_post_process_files_from_jobs(job: db_models.Job) -> List[Dict[str, Any]]:
    """Import wrapper for _get_post_process_files from jobs.py"""
    from api.routers.jobs import _get_post_process_files
    return _get_post_process_files(job)

def _get_transfer_destination_from_jobs(job: db_models.Job, db: Session) -> Optional[Dict[str, Any]]:
    """Import wrapper for _get_transfer_destination from jobs.py"""
    from api.routers.jobs import _get_transfer_destination
    return _get_transfer_destination(job, db)

def _build_job_status_from_jobs(job: db_models.Job) -> JobStatus:
    """Import wrapper for _build_job_status from jobs.py"""
    from api.routers.jobs import _build_job_status
    return _build_job_status(job)
log = logging.getLogger("api.routers.discs")
SCAN_BACKOFF_SECONDS = int(os.getenv("DISC_SCAN_BACKOFF", "8"))


def _log_rb(msg: str, **kwargs: Any) -> None:
    """Structured info for release/boxset flows (grep: release/boxset)."""
    parts = " ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items()) if v is not None)
    log.info("[release/boxset] %s%s", msg, (" " + parts) if parts else "")
_last_scan_ts: dict[str, float] = {}


# In-memory workflow context cache with short TTL (avoids repeated DB queries
# when context_changed triggers rapid refetches)
import time as _time
_workflow_context_cache: dict[str, tuple[float, Any]] = {}  # key -> (expiry_timestamp, response)
_CONTEXT_CACHE_TTL_SECONDS = 10.0


def _get_cached_context(key: str) -> Any:
    """Get cached workflow context if still fresh, or None."""
    entry = _workflow_context_cache.get(key)
    if entry and entry[0] > _time.time():
        return entry[1]
    # Expired or missing — clean up
    _workflow_context_cache.pop(key, None)
    return None


def _set_cached_context(key: str, response: Any) -> None:
    """Cache a workflow context response for a short TTL."""
    _workflow_context_cache[key] = (_time.time() + _CONTEXT_CACHE_TTL_SECONDS, response)


def invalidate_workflow_context_cache(disc_id: str | None = None, job_id: str | None = None) -> None:
    """
    Invalidate cached workflow context for a disc or job.
    Called after state changes (apply_job_state, label save, etc.).

    Note: when only `disc_id` is passed, we also wipe every `job:*` entry —
    the job-scoped workflow context is computed from disc data, and we don't
    track which job_id was built from which disc_id in this cache. The
    over-invalidation costs one rebuild per stale job after a disc change;
    that's fine given the 10s TTL would expire most entries on its own
    anyway. Same handling in the reverse direction.
    """
    if disc_id:
        _workflow_context_cache.pop(f"disc:{disc_id}", None)
        # Without a disc→job mapping, wipe all job entries to avoid serving
        # stale job context derived from the just-mutated disc.
        for key in [k for k in _workflow_context_cache.keys() if k.startswith("job:")]:
            _workflow_context_cache.pop(key, None)
    if job_id:
        _workflow_context_cache.pop(f"job:{job_id}", None)


def _merge_persisted_tmdb_suggestion(
    disc_info: Dict[str, Any],
    disc_record: db_models.Disc,
) -> None:
    """Mutate ``disc_info`` to include ``tmdb_suggestion`` from the persisted
    ``disc.disc_info`` JSON column (#388/#389).

    The workflow-context endpoints build the response's ``disc_info`` from
    the in-memory disc cache when the disc is currently inserted, falling
    back to a minimal projection of the DB row otherwise. Neither path
    reads the persisted ``disc.disc_info`` JSON column directly, which is
    where ``tmdb_suggestion`` lives — so without this merge the frontend
    never sees the suggestion and the film-step card stays hidden.

    Only the auto-identification fields are merged. Other persisted keys
    (raw_info_log, scan_tracks, …) are intentionally left out of the
    over-the-wire response shape; they were never surfaced before and
    blowing up the payload here would be a separate concern.
    """
    persisted = getattr(disc_record, "disc_info", None)
    if not isinstance(persisted, dict):
        return
    suggestion = persisted.get("tmdb_suggestion")
    if isinstance(suggestion, dict) and suggestion and not disc_info.get("tmdb_suggestion"):
        disc_info["tmdb_suggestion"] = suggestion


def _workflow_context_discdb_result(
    active_job: db_models.Job | None,
    disc_info: Optional[Dict[str, Any]] = None,
    disc_id: Optional[str] = None,
    db: Optional[Any] = None,
) -> Optional[str]:
    """Job's persisted DiscDB outcome, else derive from disc cache flags (for UI badges).

    Falls back to the most recent job on the disc (any status) when no active
    job exists — needed so the labeling UI keeps showing "DiscDB" chips on
    rows whose auto_type came from DiscDB even after the disc transferred to
    completion.
    """
    if active_job is not None:
        dr = getattr(active_job, "discdb_result", None)
        if isinstance(dr, str) and dr.strip():
            return dr.strip().lower()
    if disc_id and db is not None:
        try:
            recent = (
                db.query(db_models.Job)
                .filter(db_models.Job.disc_id == disc_id)
                .filter(db_models.Job.discdb_result.isnot(None))
                .order_by(db_models.Job.created_at.desc())
                .first()
            )
            if recent is not None:
                dr = getattr(recent, "discdb_result", None)
                if isinstance(dr, str) and dr.strip():
                    return dr.strip().lower()
        except Exception as exc:
            log.warning("Failed to lookup recent job discdb_result: %s", exc)
    if disc_info:
        if disc_info.get("discdb_hit") is True:
            return "hit"
        if disc_info.get("discdb_hit") is False:
            return "miss"
    return None


def _workflow_discdb_hit_for_disc_response(
    active_job: db_models.Job | None,
    disc_info: Dict[str, Any],
) -> bool:
    """
    Short workflow (summary-first) for WorkflowContextResponse.discdbHit.
    When discdb_miss_workflow_with_prefill is on, stage_profile is miss and label_required is True
    even if disc_info has discdb_hit — must not treat as short path from raw discdb_hit alone.
    """
    if active_job is not None:
        from api.routers.jobs import _workflow_discdb_hit_for_context

        return _workflow_discdb_hit_for_context(active_job)
    return disc_info.get("discdb_hit") is True and not bool(disc_info.get("label_required"))


def _get_titles_version(disc: db_models.Disc | None) -> int:
    if not disc:
        return 0
    label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    version = label_draft.get("titles_version")
    if isinstance(version, int):
        return version
    if isinstance(version, str) and version.isdigit():
        return int(version)
    return 0


def _set_titles_version(disc: db_models.Disc, version: int) -> None:
    label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    label_draft["titles_version"] = version
    disc.label_draft = label_draft


def _serialize_disc_title(title: db_models.DiscTitle) -> Dict[str, Any]:
    from api.crud import title_provenance_payload
    src = title.source_file or f"title_{title.id}"
    return {
        "src": src,
        "source_file": src,
        "title_id": title.id,
        "title_seq": title.title_seq,
        "title": title.title,
        "edition": getattr(title, "edition", None),
        "description": title.description,
        "type": _normalize_title_type(title.type),
        "season": title.season,
        "episode": title.episode,
        "part": title.part,
        "part_of": title.part_of,
        "episode_end": title.episode_end,
        **title_provenance_payload(title),
        "duration": title.duration,
        "duration_raw": title.duration_raw,
        "size": title.size,
        "display_size": title.display_size,
        "comment": title.comment,
        "order_index": title.order_index,
        "streams": title.streams,
        "file_path": getattr(title, "file_path", None),
        "file_path_stage": getattr(title, "file_path_stage", None),
        "active": getattr(title, "active", None),
    }


def _emit_titles_changed_threadsafe(disc_id: str, titles: List[Dict[str, Any]], titles_version: int) -> None:
    """Schedule the titles_changed WS delta from a sync endpoint.

    The PATCH endpoints are sync `def`s running in the threadpool, so
    there is no running loop here — mirror the run_coroutine_threadsafe
    dance the relookup endpoint uses. Emission is best-effort: a failed
    notification must never fail the write it describes."""
    if not titles:
        return
    try:
        from api.routers.websockets import emit_titles_changed
        coro = emit_titles_changed(disc_id, titles, titles_version)
        try:
            asyncio.get_running_loop()
            asyncio.create_task(coro)
        except RuntimeError:
            from api.main import _app_instance
            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                asyncio.run_coroutine_threadsafe(coro, _app_instance.state.event_loop)
            else:
                coro.close()
    except Exception as exc:
        log.warning(f"Failed to emit titles_changed for disc {disc_id}: {exc}")


def _apply_title_patch_fields(title: db_models.DiscTitle, fields: Dict[str, Any]) -> None:
    """Apply a dict of fields to a DiscTitle. Every provenanced label
    field (type, title, edition, description, season, episode) routes
    through `crud.set_title_field(source='user')` so the user/auto split
    and the resolved cache stay in sync — direct setattr would update
    only the legacy cache and silently lose source provenance. This is
    the single write path for all three UI editing surfaces."""
    from api.crud import PROVENANCED_TITLE_FIELDS, set_title_field
    for key, value in fields.items():
        if key == "type":
            set_title_field(title, "type", _normalize_title_type(value), source="user")
            continue
        if key in PROVENANCED_TITLE_FIELDS:
            set_title_field(title, key, value, source="user")
            continue
        if hasattr(title, key):
            setattr(title, key, value)

# Architecture Notes:
# - Drive Manager handles all disc detection and scanning (via udev events)
# - Drive Manager notifies Disc Manager when discs are inserted/ejected
# - Disc Manager enriches and caches disc metadata
# - API routes should ONLY read from Disc Manager cache (via get_cached_discs() or cache_get())
# - API routes should NOT directly call get_disc_info() unless absolutely necessary (e.g., refresh endpoint)
# - This prevents duplicate scans and maintains proper separation of concerns

def _job_to_status(job) -> JobStatus:
    payload: dict[str, Any] = {}
    disc = getattr(job, "disc", None)
    rel = getattr(disc, "release", None) if disc else None
    disc_payload_raw = getattr(job, "disc_payload", None) or {}
    disc_payload = {k: v for k, v in disc_payload_raw.items() if k not in ("label_payload", "label_draft")}
    if disc:
        payload["disc_hash"] = disc.content_hash
        payload["disc_slug"] = disc.disc_slug
        payload["disc_name"] = disc.disc_name
        payload["disc_number"] = disc.disc_number
        payload["discdb_disc_num"] = getattr(disc, "discdb_disc_num", None)
        payload["disc_format"] = disc.format
        payload["disc_id"] = str(disc.id)
    # Get production_year from movie, release_year from boxset or release
    production_year = None
    release_year = None
    resolution = None
    boxset_id = None
    movie_name = None
    if rel:
        payload["disc_group"] = rel.slug
        payload["group_type"] = rel.type
        payload["release_name"] = rel.name
        payload["release_slug"] = rel.slug
        payload["release_id"] = str(rel.id)
        boxset_id = getattr(rel, "boxset_id", None)
        payload["boxset_id"] = boxset_id
        if rel.movie:
            movie_name = rel.movie.name
            production_year = rel.movie.production_year
        # Get release_year from boxset if available, otherwise from release
        # Check boxset_id first, then try to access boxset (may not be loaded)
        if boxset_id:
            try:
                # Try to access boxset - will work if relationship is loaded
                if hasattr(rel, "boxset") and rel.boxset:
                    release_year = rel.boxset.year
            except Exception:
                # Boxset relationship not loaded, fall back to release_year
                pass
        if release_year is None:
            release_year = getattr(rel, "release_year", None)
        # Get resolution from release
        resolution = getattr(rel, "resolution", None)
    profile = (getattr(job, "stage_profile", None) or getattr(job, "discdb_result", None) or "").lower()
    default_step = "summary" if profile == "hit" else "film"
    workflow_step = getattr(job, "workflow_step", None) or default_step
    return JobStatus(
        jobId=job.id,
        disc_id=payload.get("disc_id"),
        release_id=payload.get("release_id"),
        movie_name=movie_name,
        boxset_id=boxset_id,
        release_year=release_year,
        production_year=production_year,
        resolution=resolution,
        job_status=job.job_status,
        rip_progress=job.rip_progress,
        rip_phase=getattr(job, "rip_phase", None),
        post_progress=getattr(job, "post_progress", 0),
        logs=job.logs or [],
        job_dir=str(JobPaths.for_id(str(job.id)).root),
        ripped_files=getattr(job, "ripped_files", None),
        post_paths=getattr(job, "post_paths", None),
        artifacts=getattr(disc, "artifacts", None) if disc else None,
        error_reason=getattr(job, "error_reason", None),
        titlesCompleted=getattr(job, "titles_completed", None),
        totalTitles=getattr(job, "total_titles", None),
        currentTitleProgress=getattr(job, "current_title_progress", None),
        currentTitleId=getattr(job, "current_title_id", None),
        currentTitleNumber=getattr(job, "current_title_number", None),
        perTitleProgress=getattr(job, "per_title_progress", None),
        transfer_status=getattr(job, "transfer_status", None),
        transfer_progress=getattr(job, "transfer_progress", None),
        transfer_error=getattr(job, "transfer_error", None),
        disc_hash=payload.get("disc_hash"),
        disc_payload=disc_payload or None,
        rip_state=getattr(job, "rip_state", None),
        label_state=getattr(job, "label_state", None) or _derive_pipeline(job)[0].get("label"),
        finalize_state=getattr(job, "finalize_state", None),
        post_state=job.derived_post_state,  # #365 — derived, not column
        transfer_state=getattr(job, "transfer_state", None),
        finalize_release_state=getattr(job, "finalize_release_state", None),
        label_required=bool(disc_payload.get("label_required")),
        label_ready=bool(disc_payload.get("label_ready")),
        stage_profile=getattr(job, "stage_profile", None),
        discdb_result=getattr(job, "discdb_result", None),
        dev_mode=getattr(job, "dev_mode", None),
        dev_validation=getattr(job, "dev_validation", None),
        export_path=getattr(job, "export_path", None),
        workflow_step=workflow_step,
        # Path A — segment-reorder state machine. Critical: this builder is
        # used by /discs/{id}/workflow-context which the frontend's drive
        # carousel reads from. Without this field the workspace component
        # sees stage=None and never renders the awaiting_segment_order card.
        segment_reorder_state=getattr(job, "segment_reorder_state", None),
        rip_set=getattr(job, "rip_set", None),
    )

def _get_disc_info_from_cache_or_scan(disc_num: str, mount_point: str, allow_scan: bool = False) -> dict | None:
    """
    Get disc info from cache first, only scan if cache miss and allow_scan=True.
    
    Architecture: API routes should primarily read from Disc Manager cache.
    Drive Manager handles disc detection and scanning, then notifies Disc Manager
    to cache the data. Direct scans should be rare and only when absolutely necessary.
    
    Args:
        disc_num: Disc number
        mount_point: Mount point
        allow_scan: If True, call get_disc_info() on cache miss. If False, return None.
    
    Returns:
        Disc info dict or None if not in cache and allow_scan=False
    """
    cached = cache_get(str(disc_num))
    if cached:
        cached.setdefault("disc_num", disc_num)
        cached.setdefault("mount_point", mount_point)
        return cached
    
    if allow_scan:
        return get_disc_info(str(disc_num), mount_point, refresh=False)
    
    return None


def _infer_disc_format(payload: dict) -> str | None:
    fmt = payload.get("disc_format")
    if fmt:
        return str(fmt)
    disc_type = str(payload.get("disc_type") or payload.get("type") or "").lower()
    flags = str(payload.get("drive_flags") or payload.get("libredrive_flags") or "").lower()
    if "uhd" in disc_type or "uhd" in flags:
        return "UHD"
    if "blu" in disc_type or "bd" in disc_type:
        return "Blu-Ray"
    if "dvd" in disc_type:
        return "DVD"
    res = str(payload.get("resolution") or "").lower()
    if "2160" in res or "uhd" in res:
        return "UHD"
    if res:
        return "Blu-Ray"
    return None

def _safe_disc_detail(payload: dict, default_disc_num: str, default_mount: str) -> DiscDetail:
    # Strip heavy internal fields before building DiscDetail for API response.
    # These fields are stored in disc.disc_info or job.disc_payload for worker use
    # but should not be sent to the frontend (they add ~80-100KB to discInfo).
    _STRIP_DISC_DETAIL_KEYS = frozenset({
        "raw_info_log", "info_log", "makemkv_info_log",
        "cinfo_lines", "scan_tracks", "titles_map",
        "metadata_results", "detection_results",
        "source_hashes", "source_files", "output_files",
        "title_output_map", "title_filename_map",
        "discovered_titles", "completed_titles",
        "ripped_files", "post_paths",
        # Note: "previews" is NOT stripped — frontend reads it for preview player
        # Note: "titles" kept but trimmed below — frontend reads discInfo.titles as fallback
    })
    payload = {k: v for k, v in payload.items() if k not in _STRIP_DISC_DETAIL_KEYS}
    
    # Trim heavy per-title fields from discInfo.titles to reduce duplication.
    # Full title data (with streams, chapters, metadata_scan) is in the top-level
    # titles array of the WorkflowContextResponse. discInfo.titles only needs
    # lightweight fields for the frontend's fallback title extraction.
    _TRIM_TITLE_KEYS = frozenset({
        "streams", "chapters", "chapters_info", "metadata_scan", "metadata_summary",
        "detection_flags", "detection_confidence", "detection_warning",
    })
    titles_data = payload.get("titles")
    if isinstance(titles_data, dict):
        trimmed_titles = {}
        for k, v in titles_data.items():
            if isinstance(v, dict):
                trimmed_titles[k] = {tk: tv for tk, tv in v.items() if tk not in _TRIM_TITLE_KEYS}
            else:
                trimmed_titles[k] = v
        payload["titles"] = trimmed_titles
    
    fmt = _infer_disc_format(payload)
    if fmt:
        payload = {**payload, "disc_format": fmt}
    
    # Normalize tracks to ensure required fields are present
    tracks_raw = payload.get("tracks") or {}
    tracks_normalized = {}
    if isinstance(tracks_raw, dict):
        for k, v in tracks_raw.items():
            if isinstance(v, dict):
                # Ensure required 'type' field exists, default to empty string if missing
                ti = v.get("title")
                en = v.get("episode_name")
                eff = None
                if ti is not None and str(ti).strip():
                    eff = str(ti).strip()
                elif en is not None and str(en).strip():
                    eff = str(en).strip()
                track_data = {
                    "type": v.get("type") or "",
                    "season": v.get("season"),
                    "episode": v.get("episode"),
                    "format": v.get("format"),
                    "title": eff,
                    "episode_name": eff,
                }
                tracks_normalized[str(k)] = track_data
            else:
                # If track value is not a dict, skip it
                continue
    payload["tracks"] = tracks_normalized
    
    # Ensure movie_name is set (with backward compat for show_title)
    if "movie_name" not in payload:
        payload["movie_name"] = payload.get("movie_name") or payload.get("show_title") or ""
    
    try:
        result = DiscDetail(**payload)
        return result
    except ValidationError as e:
        titles_raw = payload.get("titles") or {}
        titles_safe = {}
        if isinstance(titles_raw, dict):
            for k, v in titles_raw.items():
                titles_safe[str(k)] = v if isinstance(v, str) else json.dumps(v)
        # Derive movie_name with backward compat for show_title
        movie_name = payload.get("movie_name") or payload.get("show_title") or ""
        return DiscDetail(
            disc_num=str(payload.get("disc_num") or default_disc_num),
            mount_point=str(payload.get("mount_point") or default_mount),
            disc_id=payload.get("disc_id"),  # Include disc_id in fallback construction
            movie_name=str(movie_name),
            release_image=payload.get("release_image") or payload.get("show_image"),  # Backward compat
            tracks=tracks_normalized,
            resolution=payload.get("resolution"),
            title_type=payload.get("title_type"),
            disc_hash=payload.get("disc_hash"),
            disc_format=fmt,
            info_title=payload.get("info_title"),
            titles=titles_safe,
        )

@router.get("/")
async def list_all_discs(db: Session = Depends(database.get_db)):
    """
    List all discs with their associated drives (filters out empty drives).
    Disc-centric endpoint - returns discs, not raw drives.
    """
    try:
        # Get discs from Disc Manager
        loop = asyncio.get_running_loop()
        discs = await loop.run_in_executor(None, list_discs)
        
        # Enrich with database data
        enriched_discs = []
        for disc_info in discs:
            disc_num = disc_info.get("disc_num")
            mount_point = disc_info.get("mount_point")
            disc_hash = disc_info.get("disc_hash")
            
            if disc_hash:
                # Try to get/create disc record
                try:
                    disc_rec = crud.ensure_disc_record_from_scan(db, disc_num, mount_point, disc_info)
                    if disc_rec:
                        disc_info["disc_id"] = str(disc_rec.id)
                        disc_info["disc_number"] = disc_rec.disc_number
                        disc_info["discdb_disc_num"] = getattr(disc_rec, "discdb_disc_num", None)
                        disc_info["disc_slug"] = disc_rec.disc_slug
                        disc_info["disc_name"] = disc_rec.disc_name
                        if disc_rec.release:
                            disc_info["release_id"] = str(disc_rec.release_id)
                            disc_info["disc_group"] = disc_rec.release.slug
                except Exception as exc:
                    log.warning(f"Failed to enrich disc {disc_num} with DB data: {exc}")
            
            enriched_discs.append(disc_info)
        
        return enriched_discs
    except Exception as exc:
        log.error(f"Error listing discs: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/{disc_num}/info")
async def get_disc_info_endpoint(
    disc_num: str,
    mount_point: str,
    db: Session = Depends(database.get_db),
):
    """
    Get disc info (includes drive info).
    Disc-centric endpoint - primarily returns cached data from Disc Manager.
    
    Architecture: Reads from Disc Manager cache first. Only triggers scan if cache miss
    and explicitly needed. Drive Manager handles disc detection and scanning.
    """
    try:
        # Get disc info from cache first (no scan)
        loop = asyncio.get_running_loop()
        disc_info = await loop.run_in_executor(
            None, 
            lambda: _get_disc_info_from_cache_or_scan(str(disc_num), mount_point, allow_scan=True)
        )
        
        if not disc_info:
            raise HTTPException(404, detail=f"Disc {disc_num} not found in cache and scan not available")
        
        # Persist to database using crud module
        disc_hash = disc_info.get("disc_hash")
        if disc_hash:
            try:
                # Ensure disc_size_bytes is set when we have mount_point (fallback if scan/cache lacked it)
                if not disc_info.get("disc_size_bytes") and mount_point:
                    size = get_disc_size_bytes_for_mount_point(mount_point)
                    if size is not None:
                        disc_info["disc_size_bytes"] = size
                # During scan, only reuse existing release if disc already has one
                # Don't create new releases - that happens during labeling phase
                disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, disc_info)
                crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
                
                # Store disc scan info in disc.disc_info
                disc_scan_info = crud._extract_disc_scan_info(disc_info)
                if disc_scan_info:
                    crud._store_disc_scan_info(db, disc_record, disc_scan_info)
                
                # Enrich disc_info with database data
                disc_info["disc_id"] = str(disc_record.id)
                disc_info["disc_number"] = disc_record.disc_number
                disc_info["discdb_disc_num"] = getattr(disc_record, "discdb_disc_num", None)
                disc_info["disc_slug"] = disc_record.disc_slug
                disc_info["disc_name"] = disc_record.disc_name
                # Include info_title from database if available (may be more up-to-date than scan data)
                if disc_record.info_title:
                    disc_info["info_title"] = disc_record.info_title
                if disc_record.release_id:
                    disc_info["release_id"] = str(disc_record.release_id)
                    if disc_record.release:
                        disc_info["disc_group"] = disc_record.release.slug
                        disc_info["release_name"] = disc_record.release.name
            except Exception as exc:
                log.warning(f"Failed to persist disc to DB: {exc}")
        
        return disc_info
    except DriveManagerError as exc:
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={"type": "discdb_not_found", "message": "No TheDiscDB entry found for this disc hash"},
            ) from exc
        raise HTTPException(status_code=exc.status_code or 500, detail=str(exc)) from exc
    except Exception as exc:
        log.error(f"Error getting disc info: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.post("/{disc_id}/relookup-discdb")
def relookup_discdb_for_disc(
    disc_id: str,
    db: Session = Depends(database.get_db),
):
    """Re-run the DiscDB lookup against this disc's existing content hash
    and apply any returned enrichment to the disc + its titles. Lets the
    user recover from a stale scan (e.g. one taken while the devmode
    "DiscDB Miss" simulation was active) without deleting and re-inserting
    the disc. Internal `is_dev_mode()` gate — production returns 403 so the
    write-once invariant on `jobs.discdb_result` isn't relaxed outside
    dev. Returns `{"result": "hit"|"miss", "disc_id": ...}`.
    """
    from core.utils import is_dev_mode
    if not is_dev_mode():
        raise HTTPException(status_code=403, detail="Re-lookup is dev-mode only")

    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(status_code=404, detail="Disc not found")
    content_hash = disc.content_hash
    if not content_hash:
        raise HTTPException(status_code=400, detail="Disc has no content_hash")

    # Bypass `query_discdb`'s DB cache (it returns a release-shaped summary
    # without the `tracks` mapping when the disc already has a linked
    # release). For this dev affordance we want the FRESH TheDiscDB payload
    # so titles can be re-enriched from authoritative DB-side track data.
    from core import disc_manager
    from core.utils import retrieve_discdb_data, parse_discdb_data
    from core import settings as app_settings
    db_result: Dict[str, Any]
    try:
        if is_dev_mode() and app_settings.get_discdb_disabled():
            raise Exception("Dev mode: DiscDB disabled (simulated miss)")
        raw_db_query = retrieve_discdb_data(content_hash)
        (
            movie_name, release_image, disc_slug, db_mapping, resolution,
            disc_format, title_type, disc_group, release_year, release_date,
            original_year, original_release_date, release_discs, tmdb_id,
            release_resolution, tmdb_type, production_year,
            matched_disc_index, discdb_boxset,
        ) = parse_discdb_data(raw_db_query, content_hash)
        db_result = {
            "discdb_hit": True,
            "label_required": False,
            "label_ready": True,
            "movie_name": movie_name,
            "release_image": release_image,
            "disc_slug": disc_slug,
            "tracks": db_mapping,
            "resolution": resolution,
            "disc_format": disc_format,
            "title_type": title_type,
            "disc_group": disc_group or disc_slug,
            "group_type": title_type or "movie",
            "release_year": release_year,
            "tmdb_id": tmdb_id,
            "tmdb_type": tmdb_type,
            "production_year": production_year,
            "raw_db_query": raw_db_query,
        }
        if discdb_boxset:
            db_result["discdb_boxset"] = discdb_boxset
        if matched_disc_index is not None:
            db_result["discdb_disc_num"] = matched_disc_index
    except Exception as exc:
        log.info(f"Re-lookup DiscDB miss for disc {disc_id}: {exc}")
        return {"result": "miss", "disc_id": disc_id}

    # Overlay DiscDB track-level metadata onto existing disc_titles rows.
    # _apply_discdb_metadata_to_titles matches by source_file so the existing
    # MakeMKV-derived rows (and their indexes) stay intact while title /
    # type / season / episode / description fill in.
    db_mapping = db_result.get("tracks") or {}
    if db_mapping:
        crud._apply_discdb_metadata_to_titles(disc, db_mapping)

    # Merge the new DiscDB fields into the disc's cached disc_info so any
    # downstream consumer (workflow-context render, future label form) sees
    # the hit. Existing keys win on overlap to preserve scan-time data; new
    # DiscDB keys (movie_name, release_year, tmdb_id, discdb_hit, etc.) are
    # added.
    existing_info = disc.disc_info if isinstance(disc.disc_info, dict) else {}
    merged_info = {**db_result, **existing_info}
    merged_info["discdb_hit"] = True
    merged_info["label_required"] = db_result.get("label_required", False)
    merged_info["label_ready"] = db_result.get("label_ready", True)
    disc.disc_info = merged_info

    # Flip jobs.discdb_result on this disc's jobs to 'hit'. We intentionally
    # relax the write-once invariant for the dev-only relookup affordance.
    jobs = db.query(db_models.Job).filter(db_models.Job.disc_id == disc_id).all()
    for j in jobs:
        j.discdb_result = "hit"

    db.commit()
    db.refresh(disc)

    invalidate_workflow_context_cache(disc_id=disc_id)

    try:
        from api.routers.websockets import _emit_to_disc_workflow
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_disc_workflow(disc_id, changed_fields=['titlesList', 'labelForm']))
        except RuntimeError:
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(
                        _emit_to_disc_workflow(disc_id, changed_fields=['titlesList', 'labelForm']),
                        loop,
                    )
            except Exception as exc:
                log.warning(f"Failed to schedule WS emission for disc {disc_id}: {exc}")
    except Exception as exc:
        log.warning(f"Failed to emit WS context change for disc {disc_id}: {exc}")

    return {"result": "hit", "disc_id": disc_id}


@router.post("/{disc_num}/refresh")
async def refresh_disc_info_endpoint(
    disc_num: str,
    mount_point: str,
    db: Session = Depends(database.get_db),
):
    """
    Refresh disc info (only if no active operations).
    Disc-centric endpoint - uses Disc Manager, then persists to DB.
    """
    try:
        # Get refreshed disc info from Disc Manager
        loop = asyncio.get_running_loop()
        disc_info = await loop.run_in_executor(None, lambda: refresh_disc_info(str(disc_num), mount_point))
        
        # Persist to database using crud module
        disc_hash = disc_info.get("disc_hash")
        if disc_hash:
            try:
                # Ensure disc_size_bytes is set when we have mount_point (fallback if scan lacked it)
                if not disc_info.get("disc_size_bytes") and mount_point:
                    size = get_disc_size_bytes_for_mount_point(mount_point)
                    if size is not None:
                        disc_info["disc_size_bytes"] = size
                # During scan, only reuse existing release if disc already has one
                # Don't create new releases - that happens during labeling phase
                disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, disc_info)
                crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
                
                # Store disc scan info in disc.disc_info
                disc_scan_info = crud._extract_disc_scan_info(disc_info)
                if disc_scan_info:
                    crud._store_disc_scan_info(db, disc_record, disc_scan_info)
                
                # Enrich disc_info with database data
                disc_info["disc_id"] = str(disc_record.id)
                disc_info["disc_number"] = disc_record.disc_number
                disc_info["discdb_disc_num"] = getattr(disc_record, "discdb_disc_num", None)
                disc_info["disc_slug"] = disc_record.disc_slug
                disc_info["disc_name"] = disc_record.disc_name
                # Include info_title from database if available (may be more up-to-date than scan data)
                if disc_record.info_title:
                    disc_info["info_title"] = disc_record.info_title
                if disc_record.release_id:
                    disc_info["release_id"] = str(disc_record.release_id)
                    if disc_record.release:
                        disc_info["disc_group"] = disc_record.release.slug
                        disc_info["release_name"] = disc_record.release.name
            except Exception as exc:
                log.warning(f"Failed to persist disc to DB: {exc}")
        
        return disc_info
    except DriveManagerError as exc:
        if exc.status_code == 409:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if exc.status_code == 404:
            raise HTTPException(
                status_code=404,
                detail={"type": "discdb_not_found", "message": "No TheDiscDB entry found for this disc hash"},
            ) from exc
        raise HTTPException(status_code=exc.status_code or 500, detail=str(exc)) from exc
    except Exception as exc:
        log.error(f"Error refreshing disc info: {exc}")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

def _labelform_to_ops(labelForm: Dict[str, Any], is_partial: bool = False) -> List[Dict[str, Any]]:
    """
    Convert labelForm data to ops format for patch_disc_ops.
    Mirrors the logic from frontend buildOpsPayload.
    
    Args:
        labelForm: Label form data from frontend
        is_partial: If True, only include changed fields (for PATCH)
    
    Returns:
        List of ops in format: [{"target": "release", "fields": {...}}, ...]
    """
    ops = []
    
    # Release ops (only when explicit release edits are present)
    release_fields = {}
    release_update_intent = bool(labelForm.get("release_id"))
    if labelForm.get("release_name"):
        release_fields["release_name"] = labelForm["release_name"]
        release_update_intent = True
    if labelForm.get("release_year") is not None:
        release_fields["release_year"] = labelForm["release_year"]
        release_update_intent = True
    if "boxset_id" in labelForm:
        release_fields["boxset_id"] = labelForm["boxset_id"]
        release_update_intent = True
    if labelForm.get("upc"):
        release_fields["upc"] = labelForm["upc"]
        release_update_intent = True
    if labelForm.get("asin"):
        release_fields["asin"] = labelForm["asin"]
        release_update_intent = True
    if labelForm.get("cover_front_url"):
        release_fields["cover_front_url"] = labelForm["cover_front_url"]
        release_update_intent = True
    if labelForm.get("cover_back_url"):
        release_fields["cover_back_url"] = labelForm["cover_back_url"]
        release_update_intent = True
    if labelForm.get("recalculate_disc_numbers"):
        release_fields["recalculate_disc_numbers"] = labelForm["recalculate_disc_numbers"]
        release_update_intent = True
    if release_update_intent:
        if "movie_id" in labelForm:
            release_fields["movie_id"] = labelForm.get("movie_id")
        if labelForm.get("tmdb_id"):
            release_fields["tmdb_id"] = labelForm["tmdb_id"]
        if labelForm.get("group_type"):
            release_fields["group_type"] = labelForm["group_type"]
    
    if release_fields:
        ops.append({"target": "release", "fields": release_fields})
    
    # Disc ops
    disc_fields = {}
    if labelForm.get("disc_name"):
        disc_fields["disc_name"] = labelForm["disc_name"]
        raw_slug = labelForm.get("disc_slug")
        disc_fields["disc_slug"] = "" if raw_slug is None else str(raw_slug).strip()
    elif "disc_slug" in labelForm:
        raw_slug = labelForm.get("disc_slug")
        disc_fields["disc_slug"] = "" if raw_slug is None else str(raw_slug).strip()
    if labelForm.get("disc_format"):
        disc_fields["disc_format"] = labelForm["disc_format"]
    if labelForm.get("disc_number") is not None:
        disc_fields["disc_number"] = labelForm["disc_number"]
    if "release_id" in labelForm:
        disc_fields["release_id"] = labelForm.get("release_id")
    
    if disc_fields:
        ops.append({"target": "disc", "fields": disc_fields})
    
    # Title ops (from titles array - preferred, or tracks array for backward compatibility)
    titles_data = labelForm.get("titles") or labelForm.get("tracks") or []
    for title_data in titles_data:
        # Use title_id, source_file, or track_id to identify the title
        title_id = title_data.get("title_id") or title_data.get("source_file") or title_data.get("track_id")
        if not title_id:
            continue
        
        title_fields = {}
        if title_data.get("title") is not None:
            title_fields["title"] = title_data["title"]
        if title_data.get("description") is not None:
            title_fields["description"] = title_data["description"]
        if title_data.get("comment") is not None:
            title_fields["comment"] = title_data["comment"]
        if title_data.get("season") is not None:
            title_fields["season"] = title_data["season"]
        if title_data.get("episode") is not None:
            title_fields["episode"] = title_data["episode"]
        if title_data.get("type") is not None:
            title_fields["type"] = _normalize_title_type(title_data["type"])
        if title_data.get("duration") is not None:
            title_fields["duration"] = title_data["duration"]
        if title_data.get("size") is not None:
            title_fields["size"] = title_data["size"]
        if title_data.get("title_seq") is not None:
            title_fields["title_seq"] = title_data["title_seq"]
        
        if title_fields:
            # Find the title record by source_file or title_id
            # The op_id should be the database ID, but we can look it up by source_file
            # For now, use source_file as the identifier and let _patch_disc_ops_internal handle it
            # Actually, we need the database ID - let's use source_file and the backend will look it up
            ops.append({
                "target": "title",
                "id": title_id,  # This will be used to find the title by source_file
                "fields": title_fields
            })
    
    # label_draft holds disc-scoped editing state pre-finalize: movie_id,
    # group_type, and primary_season (#371 disc-card season pick, persisted
    # in #536). Release/boxset data still lives on Release/Boxset.
    label_draft_fields = {}
    if "movie_id" in labelForm:
        label_draft_fields["movie_id"] = labelForm.get("movie_id")
    if "group_type" in labelForm:
        label_draft_fields["group_type"] = labelForm.get("group_type")
    if "primary_season" in labelForm:
        raw_season = labelForm.get("primary_season")
        try:
            season_int = int(raw_season) if raw_season is not None else None
        except (TypeError, ValueError):
            season_int = None
        # Drop garbage / non-positive values rather than rejecting the whole op.
        label_draft_fields["primary_season"] = season_int if (season_int and season_int > 0) else None

    if label_draft_fields:
        ops.append({"target": "label_draft", "fields": label_draft_fields})

    return ops


def _workflow_pending_release_summary(
    db: Session,
    disc_record: db_models.Disc,
) -> Optional[ReleaseSummary]:
    """DiscDB candidate release while disc.release_id is still null."""
    from api.routers.releases import _release_summary

    if disc_record.release_id:
        return None
    info = disc_record.disc_info if isinstance(disc_record.disc_info, dict) else {}
    pid = info.get("pending_release_id")
    if not pid:
        return None
    pr = (
        db.query(db_models.Release)
        .options(
            joinedload(db_models.Release.movie),
            joinedload(db_models.Release.boxset),
        )
        .filter(db_models.Release.id == str(pid))
        .first()
    )
    if not pr:
        return None
    return _release_summary(pr, db)


def _linked_movie_tmdb_id(disc_record: db_models.Disc) -> str:
    """TMDB id reached through the disc's own link: disc -> release -> movie.

    ``labelForm.tmdb_id`` used to come only from ``disc_info`` (the scan
    payload). A disc labeled through the normal flow has its series recorded
    on the linked Movie, not in disc_info — so on resume the field came back
    empty, the frontend's `_prefetchTmdbEpisodeCatalog` returned early on
    `if (!tmdb_id) return`, no episode catalog was ever fetched, and the TMDB
    episode picker silently rendered nothing on every Episode row.

    Observed on Star Wars Rebels S3: disc -> release -> movie carried
    tmdb_id 60554 and `GET /movies/60554/seasons/3/episodes` returned the
    full season, while labelForm.tmdb_id was "".

    Read-only and lowest-precedence — callers must prefer any explicitly
    persisted value so a hand-linked disc is never overridden.
    """
    release = getattr(disc_record, "release", None)
    movie = getattr(release, "movie", None) if release is not None else None
    tmdb_id = getattr(movie, "tmdb_id", None) if movie is not None else None
    return str(tmdb_id) if tmdb_id else ""


def _build_labelform_from_disc(
    disc_record: db_models.Disc,
    disc_info: Dict[str, Any],
    active_job: db_models.Job | None = None,
    db: Session | None = None,
) -> Dict[str, Any]:
    """
    Build labelForm from disc record and disc info.
    Mirrors frontend buildLabelForm logic.
    """
    # Get label_draft if available (label fields only; workflow_step is job-scoped)
    label_draft = disc_record.label_draft if isinstance(disc_record.label_draft, dict) else {}
    latest_job = None
    try:
        if getattr(disc_record, "jobs", None):
            latest_job = sorted(disc_record.jobs, key=lambda j: j.created_at)[-1]
    except Exception:
        latest_job = None

    job_step = None
    if active_job is not None:
        job_step = getattr(active_job, "workflow_step", None)
    if job_step is None and latest_job is not None:
        job_step = getattr(latest_job, "workflow_step", None)

    if active_job is not None:
        from api.routers.jobs import _workflow_discdb_hit_for_context

        short_workflow = _workflow_discdb_hit_for_context(active_job)
    else:
        short_workflow = disc_info.get("discdb_hit") is True and not bool(disc_info.get("label_required"))
    default_step = "summary" if short_workflow else "film"
    workflow_step = job_step or default_step
    
    # Prefer release.type for group_type when release exists, else label_draft then disc_info
    _group_type = "movie"
    if disc_record.release and getattr(disc_record.release, "type", None):
        _group_type = (disc_record.release.type or "movie").lower()
    else:
        _group_type = (label_draft.get("group_type") or disc_info.get("title_type") or "movie").lower()
    # Build base form. release_* come from disc_record.release or disc_info; label_draft holds only movie_id and group_type.
    form: Dict[str, Any] = {
        "mode": _group_type,
        "group_type": _group_type,
        "disc_group": disc_record.release.slug if disc_record.release else "",
        "disc_number": disc_record.disc_number,
        "discdb_disc_num": getattr(disc_record, "discdb_disc_num", None),
        # disc_info first (scan payload), then the disc's own movie link. The
        # fallback only fills an otherwise-empty value; it never overrides one.
        "tmdb_id": disc_info.get("tmdb_id") or _linked_movie_tmdb_id(disc_record) or "",
        "disc_format": disc_record.format or disc_info.get("disc_format"),
        "release_name": "",
        "release_slug": disc_record.release.slug if disc_record.release else "",
        "info_title": disc_record.info_title or disc_info.get("info_title"),
        "upc": None,
        "asin": None,
        "cover_front_url": None,
        "cover_back_url": None,
        "release_year": disc_info.get("release_year"),
        "production_year": disc_info.get("production_year"),
        "disc_name": disc_record.disc_name or "",
        "disc_slug": disc_record.disc_slug or "",
        # Respect explicit clear: if movie_id is in label_draft (including None), use it; else release or disc_info
        "movie_id": (
            (str(label_draft["movie_id"]) if label_draft["movie_id"] else None) if "movie_id" in label_draft
            else (str(disc_record.release.movie_id) if disc_record.release and disc_record.release.movie_id
                  else disc_info.get("movie_id"))
        ),
        "boxset_id": disc_record.release.boxset_id if disc_record.release else None,
        # Disc-card primary-season pick (#371). Persisted in label_draft so it
        # survives reload / websocket refetch; frontend's seed precedence
        # prefers this over the TMDB title-pattern hint.
        "primary_season": (
            int(label_draft["primary_season"])
            if isinstance(label_draft.get("primary_season"), (int, float))
            and not isinstance(label_draft.get("primary_season"), bool)
            and label_draft.get("primary_season") > 0
            else None
        ),
        "workflow_step": workflow_step,
        "tracks": [],
    }
    
    # Add release fields if release exists
    if disc_record.release:
        rel = disc_record.release
        rel_movie_id = str(rel.movie_id) if rel.movie_id else None
        rel_id_str = str(rel.id)
        
        # Detect stale label_draft: if disc.release_id exists but label_draft has different release_id
        label_draft_release_id = str(label_draft.get("release_id")) if label_draft.get("release_id") else None
        is_stale_release = (
            "release_id" in label_draft and 
            label_draft_release_id and 
            label_draft_release_id != rel_id_str
        )
        
        # Detect stale movie_id: if disc.release.movie_id exists but label_draft has different movie_id
        label_draft_movie_id = str(label_draft.get("movie_id")) if label_draft.get("movie_id") else None
        is_stale_movie = (
            "movie_id" in label_draft and 
            label_draft_movie_id and 
            rel_movie_id and 
            label_draft_movie_id != rel_movie_id
        )
        
        # DEBUG: Log label_draft vs disc.release comparison
        logging.info(
            f"[DEBUG] _build_labelform_from_disc - disc {disc_record.id}: "
            f"label_draft keys={list(label_draft.keys())}, "
            f"label_draft.movie_id={label_draft.get('movie_id')}, "
            f"label_draft.release_id={label_draft.get('release_id')}, "
            f"disc.release_id={disc_record.release_id}, "
            f"disc.release.movie_id={rel.movie_id}, "
            f"disc.release.boxset_id={rel.boxset_id}, "
            f"is_stale_release={is_stale_release}, "
            f"is_stale_movie={is_stale_movie}"
        )
        
        # Always set release_id from disc.release (it's the source of truth)
        form["release_id"] = rel_id_str
        form["release_slug"] = rel.slug
        form["disc_group"] = rel.slug
        
        # Prefer release.type for group_type/mode so type selection shows release type
        if getattr(rel, "type", None):
            form["group_type"] = (rel.type or "movie").lower()
            form["mode"] = form["group_type"]
        
        # For stale label_draft, always use disc.release.movie_id as source of truth
        if is_stale_movie:
            logging.warning(
                f"[DEBUG] _build_labelform_from_disc - disc {disc_record.id}: "
                f"Stale movie_id in label_draft! Using disc.release.movie_id as source of truth. "
                f"label_draft.movie_id={label_draft_movie_id}, disc.release.movie_id={rel_movie_id}"
            )
            form["movie_id"] = rel_movie_id
        elif rel.movie_id and "movie_id" not in label_draft:
            # Only fill movie_id from release when we didn't get it from label_draft
            form["movie_id"] = str(rel.movie_id)
            logging.info(
                f"[DEBUG] _build_labelform_from_disc - disc {disc_record.id}: "
                f"Applied movie_id from disc.release: {rel.movie_id}"
            )
        elif "movie_id" in label_draft and not is_stale_movie:
            logging.info(
                f"[DEBUG] _build_labelform_from_disc - disc {disc_record.id}: "
                f"NOT applying movie_id from disc.release (movie_id in label_draft). "
                f"label_draft.movie_id={label_draft.get('movie_id')}, disc.release.movie_id={rel.movie_id}"
            )
        
        if rel and rel.movie and rel.movie.tmdb_id:
            form["tmdb_id"] = form["tmdb_id"] or rel.movie.tmdb_id
        form["release_name"] = rel.name or ""
        form["release_year"] = rel.release_year
        form["upc"] = rel.upc
        form["asin"] = rel.asin
        form["cover_front_url"] = rel.cover_front_url
        form["cover_back_url"] = rel.cover_back_url
        if rel.boxset_id:
            form["boxset_id"] = rel.boxset_id

    elif db is not None:
        info = disc_record.disc_info if isinstance(disc_record.disc_info, dict) else {}
        pid = info.get("pending_release_id") or disc_info.get("pending_release_id")
        if pid:
            form["pending_release_id"] = str(pid)
            form["release_missing_required_fields"] = list(
                info.get("release_missing_required_fields")
                or disc_info.get("release_missing_required_fields")
                or []
            )
            form["release_link_ready"] = False
            pr = (
                db.query(db_models.Release)
                .options(joinedload(db_models.Release.movie), joinedload(db_models.Release.boxset))
                .filter(db_models.Release.id == str(pid))
                .first()
            )
            if pr:
                form["release_name"] = pr.name or ""
                form["release_slug"] = pr.slug or ""
                form["disc_group"] = pr.slug or ""
                form["release_year"] = pr.release_year
                form["upc"] = pr.upc
                form["asin"] = pr.asin
                form["cover_front_url"] = pr.cover_front_url
                form["cover_back_url"] = pr.cover_back_url
                if pr.boxset_id:
                    form["boxset_id"] = pr.boxset_id
                if getattr(pr, "type", None):
                    form["group_type"] = (pr.type or "movie").lower()
                    form["mode"] = form["group_type"]
                if pr.movie and pr.movie.tmdb_id:
                    form["tmdb_id"] = form.get("tmdb_id") or pr.movie.tmdb_id

    # Add tracks/titles from disc record
    if disc_record.titles:
        form["tracks"] = []
        for title in disc_record.titles:
            form["tracks"].append({
                "source_file": title.source_file or "",
                "track_id": title.source_file or "",
                "title_id": str(title.id) if title.id else None,
                "disc_track_id": None,  # Will be set if needed
                "title": title.title or "",
                "description": title.description or "",
                "note": title.description or "",
                "comment": title.comment,
                "season": title.season,
                "episode": title.episode,
                "part": title.part,
                "part_of": title.part_of,
                "episode_end": title.episode_end,
                "type": _normalize_title_type(title.type) or "",
                "duration": title.duration,
                "size": None,  # Not stored in title record
            })
    
    # DEBUG: Log final form values
    logging.info(
        f"[DEBUG] _build_labelform_from_disc - disc {disc_record.id}: Final form - "
        f"movie_id={form.get('movie_id')}, "
        f"release_id={form.get('release_id')}, "
        f"release_name={form.get('release_name')}, "
        f"release_slug={form.get('release_slug')}, "
        f"boxset_id={form.get('boxset_id')}, "
        f"workflow_step={form.get('workflow_step')}"
    )
    
    return form


def _get_release_discs_for_disc(release: db_models.Release, db: Session) -> List[Dict[str, Any]]:
    """
    Get all discs in a release (for disc workflow context).
    """
    if not release:
        return []
    
    release_ids = [release.id]
    if getattr(release, "boxset_id", None):
        releases_in_boxset = (
            db.query(db_models.Release)
            .filter(db_models.Release.boxset_id == release.boxset_id)
            .all()
        )
        release_ids = [rel.id for rel in releases_in_boxset] or release_ids
    
    # Titles are not read by this function — drop the joinedload to avoid materialising
    # hundreds of disc_titles rows (with their JSONB metadata_scan/streams payloads) for
    # each disc in the release. Jobs uses selectinload to dodge the Cartesian product
    # against any other collection. Release stays joinedload (many-to-one, single row).
    discs = (
        db.query(db_models.Disc)
        .options(selectinload(db_models.Disc.jobs), joinedload(db_models.Disc.release))
        .filter(db_models.Disc.release_id.in_(release_ids))
        .all()
    )
    
    release_discs: List[Dict[str, Any]] = []
    for disc in discs:
        latest_job = None
        if disc.jobs:
            latest_job = max(
                disc.jobs,
                key=lambda j: j.created_at.timestamp() if j.created_at else 0
            )
        
        # Build lightweight job summary instead of full _build_job_status() (~110KB per disc).
        # The disc step only needs progress/state fields for display.
        latest_job_status = None
        if latest_job:
            latest_job_status = {
                "jobId": str(latest_job.id),
                "job_status": latest_job.job_status,
                "rip_state": getattr(latest_job, "rip_state", None),
                "post_state": latest_job.derived_post_state,  # #365 — derived, not column
                "transfer_state": getattr(latest_job, "transfer_state", None),
                "label_state": getattr(latest_job, "label_state", None),
                "rip_progress": latest_job.rip_progress,
                "post_progress": getattr(latest_job, "post_progress", 0),
                "transfer_progress": getattr(latest_job, "transfer_progress", None),
                "workflow_step": getattr(latest_job, "workflow_step", None),
                "error_reason": getattr(latest_job, "error_reason", None),
                "stage_profile": getattr(latest_job, "stage_profile", None),
            }
        
        entry: Dict[str, Any] = {
            "disc_id": str(disc.id),
            "disc_number": disc.disc_number,
            "discdb_disc_num": getattr(disc, "discdb_disc_num", None),
            "disc_name": disc.disc_name,
            "disc_slug": disc.disc_slug,
            "disc_format": disc.format,
            "content_hash": disc.content_hash,
            "release_id": str(disc.release_id) if disc.release_id else None,
            "release_name": disc.release.name if disc.release else None,
        }
        
        if latest_job_status:
            entry["latest_job_status"] = latest_job_status
        
        release_discs.append(entry)
    
    return release_discs


def _get_boxset_movies_for_disc(boxset: db_models.Boxset, db: Session) -> List[Dict[str, Any]]:
    """
    Get all movies in a boxset (for disc workflow context).
    Movies are accessed through releases (Boxset -> Release -> Movie).
    """
    if not boxset:
        return []
    
    # Query releases in the boxset and get their movies
    releases = db.query(db_models.Release).options(
        joinedload(db_models.Release.movie)
    ).filter(db_models.Release.boxset_id == boxset.id).all()
    
    # Collect unique movies from releases
    movies_dict = {}
    for release in releases:
        if release.movie and release.movie.id not in movies_dict:
            movies_dict[release.movie.id] = {
                "id": str(release.movie.id),
                "name": release.movie.name,
                "cover_url": release.movie.cover_url,
                "cover_path": release.movie.cover_path,
                "production_year": release.movie.production_year,
                "tmdb_id": release.movie.tmdb_id,
            }
    
    return list(movies_dict.values())


def _get_release_details_for_disc(release: db_models.Release) -> Optional[Dict[str, Any]]:
    """
    Build full release details object (for disc workflow context).
    """
    if not release:
        return None
    
    details: Dict[str, Any] = {
        "id": str(release.id),
        "name": release.name,
        "slug": release.slug,
        "type": release.type,
        "release_year": release.release_year,
        "upc": release.upc,
        "asin": release.asin,
        "cover_front_url": release.cover_front_url,
        "cover_back_url": release.cover_back_url,
        "tmdb_id": release.movie.tmdb_id if release.movie else None,
    }
    
    # Add movie info if available
    if release.movie:
        details["movie_id"] = str(release.movie.id) if release.movie.id else None
        details["movie_name"] = release.movie.name
        details["movie_cover_url"] = release.movie.cover_url
        details["movie_production_year"] = release.movie.production_year
    
    # Add boxset info if available
    if release.boxset_id:
        details["boxset_id"] = str(release.boxset_id)
    
    return details


def _load_workflow_options(db: Session) -> Dict[str, Any]:
    """
    Load all options needed for workflow context (movies, boxsets, releases, groups).
    
    Performance: Uses eager loading to avoid N+1 queries. The Release query eager-loads
    Movie, Boxset, Discs, and Disc→Jobs in a single query batch so _release_summary()
    doesn't trigger lazy loads.
    """
    # Load movies
    movies = db.query(db_models.Movie).order_by(db_models.Movie.name).all()
    movie_options = [
        MovieSummary(
            id=m.id,
            name=m.name,
            production_year=m.production_year,
            tmdb_id=m.tmdb_id,
            tmdb_type=m.tmdb_type,
            cover_url=m.cover_url,
            cover_path=m.cover_path,
        )
        for m in movies
    ]
    
    # Load boxsets with eager loading for release count (avoid N+1 queries)
    boxsets = (
        db.query(db_models.Boxset)
        .options(selectinload(db_models.Boxset.releases))
        .order_by(db_models.Boxset.name)
        .all()
    )
    boxset_options = []
    for b in boxsets:
        # Use the eager-loaded relationship instead of a separate query
        release_count = len(b.releases) if hasattr(b, 'releases') else 0
        boxset_options.append(
            BoxsetSummary(
                id=b.id,
                slug=b.slug,
                name=b.name,
                title=b.title,
                sort_title=b.sort_title,
                upc=b.upc,
                asin=b.asin,
                year=b.year,
                locale=b.locale,
                region_code=b.region_code,
                cover_front_url=b.cover_front_url,
                cover_back_url=b.cover_back_url,
                image_url=b.image_url,
                release_date=b.release_date,
                finalized=b.finalized,
                finalized_at=b.finalized_at,
                release_count=release_count,
            )
        )
    
    # Load releases with FULL eager loading chain to avoid N+1 queries.
    # _release_summary() accesses rel.discs -> disc.jobs, so we must eager-load
    # the entire chain: Release -> Movie, Boxset, Discs -> Jobs
    releases = (
        db.query(db_models.Release)
        .options(
            joinedload(db_models.Release.movie),
            joinedload(db_models.Release.boxset),
            selectinload(db_models.Release.discs).selectinload(db_models.Disc.jobs),
        )
        .order_by(db_models.Release.updated_at.desc())
        .all()
    )
    # Import helper function
    from api.routers.releases import _release_summary
    
    # Build release summaries ONCE (was being computed twice before - once for
    # releaseOptions and once for groupOptions)
    release_summaries = []
    for rel in releases:
        release_summaries.append(_release_summary(rel, db))
    
    release_options = list(release_summaries)
    
    # Group options built from already-computed summaries (no second iteration)
    group_options = []
    for rel_summary in release_summaries:
        group_options.append({
            "release_id": rel_summary.id,
            "disc_group": rel_summary.slug,
            "group_type": rel_summary.type,
            "release_name": rel_summary.name or None,
            "release_slug": rel_summary.slug,
            "movie_id": rel_summary.movie_id,
            "movie": rel_summary.movie.model_dump() if rel_summary.movie else None,
            "resolution": rel_summary.resolution,
            "tmdb_id": rel_summary.tmdb_id,
            "upc": rel_summary.upc,
            "asin": rel_summary.asin,
            "cover_front_url": rel_summary.cover_front_url,
            "cover_back_url": rel_summary.cover_back_url,
            "release_year": rel_summary.release_year,
            "production_year": rel_summary.production_year,
        })
    
    return {
        "movieOptions": movie_options,
        "boxsetOptions": boxset_options,
        "releaseOptions": release_options,
        "groupOptions": group_options,
    }


@router.get("/options")
def get_cached_workflow_options(db: Session = Depends(database.get_db)):
    """
    Dedicated endpoint for workflow options (movies, boxsets, releases, groups).
    
    Returns reference data needed by the workflow UI. Results are cached in Redis
    with a 60-second TTL. The cache is invalidated when movies, boxsets, or releases
    are created, updated, or deleted.
    
    This endpoint replaces the options that were previously embedded in every
    workflow context response, reducing context response size and eliminating
    redundant database queries.
    """
    from core.redis_cache import get as redis_get, set as redis_set
    
    # Try Redis cache first
    cached = redis_get("options", "workflow_options")
    if cached:
        return cached
    
    # Cache miss: load from database
    options = _load_workflow_options(db)
    
    # Serialize Pydantic models for Redis storage
    serialized = {
        "movieOptions": [o.model_dump(mode='json') if hasattr(o, 'model_dump') else o for o in options["movieOptions"]],
        "boxsetOptions": [o.model_dump(mode='json') if hasattr(o, 'model_dump') else o for o in options["boxsetOptions"]],
        "releaseOptions": [o.model_dump(mode='json') if hasattr(o, 'model_dump') else o for o in options["releaseOptions"]],
        "groupOptions": options["groupOptions"],
    }
    
    # Cache for 60 seconds (stale for 120 seconds)
    redis_set("options", "workflow_options", serialized, ttl=60, stale_ttl=120)
    
    return serialized


def invalidate_options_cache() -> None:
    """
    Invalidate the Redis-cached workflow options.
    Call this after any mutation to movies, boxsets, or releases.
    """
    from core.redis_cache import invalidate as redis_invalidate
    redis_invalidate("options")


async def _emit_options_changed() -> None:
    """
    Emit an options_changed WebSocket event so the frontend refreshes its cached options.
    """
    try:
        from api.routers.websockets import _emit_unified
        await _emit_unified({"type": "options_changed"})
    except Exception as exc:
        log.warning(f"Failed to emit options_changed: {exc}")


def _normalize_mount_point(mount_point: str) -> str:
    """
    Normalize mount_point so malformed encoding from clients/proxies still matches cache.
    E.g. %F instead of %2F for '/' can produce literal '%F' or a single char; fix path slashes.
    """
    if not mount_point or not isinstance(mount_point, str):
        return mount_point
    # Fix common mangling: literal %F (invalid encoding for /) or backslashes
    s = mount_point.replace("%F", "/").replace("\\", "/")
    # Collapse multiple slashes and strip
    while "//" in s:
        s = s.replace("//", "/")
    return s.strip() or mount_point


@router.get("/workflow-context", response_model=WorkflowContextResponse)
def get_disc_workflow_context_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    db: Session = Depends(database.get_db),
    include: str = Query("label,titles,job,release,discinfo", description="Comma-separated fields to include: label,titles,job,release,discinfo"),
):
    """
    Get workflow context for a disc using mount_point (before disc_id is available).
    Returns disc_id in response if available, otherwise uses mount_point as id.
    
    Architecture: Reads from Disc Manager cache only. Drive Manager handles disc detection
    and scanning, then notifies Disc Manager to cache the data. This endpoint should not
    trigger scans directly.
    
    Note: This is a sync endpoint (def, not async def) so FastAPI runs it in a threadpool.
    This prevents blocking the event loop during synchronous DB operations, which was
    causing all concurrent requests to serialize (~93s queue times).
    """
    mount_point = _normalize_mount_point(mount_point)
    # 1. Find disc by mount_point from cache (no scan)
    cached_discs = get_cached_discs()
    
    disc_match = None
    for disc_info in cached_discs:
        if disc_info.get("mount_point") == mount_point:
            disc_match = disc_info
            break
    
    if not disc_match:
        # Disc not in cache - return minimal "shell" workflow context
        # This allows the frontend to render the workflow even for newly inserted discs
        # Options are NOT loaded here - frontend fetches them separately via GET /discs/options
        return WorkflowContextResponse(
            id=mount_point,
            type="disc",
            discId=None,
            mountPoint=mount_point,
            discNum=None,
            labelForm={
                "mode": "movie",
                "group_type": "movie",
                "disc_group": "",
                "disc_number": None,
                "tmdb_id": "",
                "disc_format": None,
                "release_name": "",
                "release_slug": "",
                "info_title": None,
                "upc": None,
                "asin": None,
                "cover_front_url": None,
                "cover_back_url": None,
                "release_year": None,
                "production_year": None,
                "disc_name": "",
                "disc_slug": "",
                "movie_id": None,
                "boxset_id": None,
                "workflow_step": "film",
                "tracks": [],
            },
            titles=[],
            titleOrder=[],
            jobStatus=None,
            discInfo=None,  # No discInfo available when disc not in cache
            movieOptions=[],
            boxsetOptions=[],
            releaseOptions=[],
            groupOptions=[],
            labelDraftProcessed=False,
            discNameLocked=False,
            discSlugLocked=False,
            isSeries=False,
            discdbHit=False,
            discdb_result=None,
            discMode="copy",
            lastReleaseDetails=None,
            releaseNameHint="",
            releaseSlugHint="",
            postProcessFiles=[],
            transferDestination=None,
            releaseDiscs=[],
            boxsetMovies=[],
            movieCover=None,
            movieName=None,
            productionYear=None,
        )
    
    # Parse include parameter for deferred loading
    include_set = set(s.strip() for s in include.split(",")) if include else set()
    include_titles = "titles" in include_set
    include_release = "release" in include_set
    include_discinfo = "discinfo" in include_set
    include_job = "job" in include_set
    
    # Use cached disc info (no scan triggered)
    disc_info = disc_match
    disc_num = disc_info.get("disc_num")
    
    if not disc_num:
        raise HTTPException(404, detail=f"Disc info incomplete for mount_point {mount_point}")
    
    # 3. Load disc record from DB (single query with eager loads).
    # NO DB writes here — disc creation/scan info storage happens during disc scan,
    # not on every GET request. This is a pure read operation.
    disc_hash = disc_info.get("disc_hash") or disc_info.get("content_hash")
    disc_record = None
    if disc_hash:
        # Use selectinload for titles — joinedload combined with the release+movie+
        # boxset joins produces a Cartesian-product result row count and causes
        # psycopg2 to JSON-decode all 200+ title rows for every workflow-context
        # call on Midway-class discs (~30s+ per call). selectinload fires a
        # separate IN-query that's ~constant time regardless of join breadth.
        # (Same fix as the disc-id endpoint below.)
        load_options = [
            joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
            joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
        ]
        if include_titles:
            load_options.append(selectinload(db_models.Disc.titles))
        disc_record = (
            db.query(db_models.Disc)
            .options(*load_options)
            .filter(db_models.Disc.content_hash == disc_hash)
            .first()
        )
    if disc_record:
        crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
        # Surface persisted TMDB auto-suggestion (#388) so the film step (#389)
        # can render the suggestion card on a disc that's currently in the
        # drive but whose cache entry predates the suggestion being written.
        _merge_persisted_tmdb_suggestion(disc_info, disc_record)

    disc_id = str(disc_record.id) if disc_record else None

    active_job = None
    if disc_id:
        try:
            active_job = (
                db.query(db_models.Job)
                .filter(db_models.Job.disc_id == disc_id)
                .filter(db_models.Job.job_status.in_(["pending", "running", "validating"]))
                .order_by(db_models.Job.created_at.desc())
                .first()
            )
        except Exception as exc:
            log.warning(f"Failed to get active job for disc {disc_id} (mount workflow context): {exc}")

    # 4. Build labelForm from disc record and disc info
    labelForm = None
    pending_release = None
    if disc_record:
        labelForm = _build_labelform_from_disc(disc_record, disc_info, active_job=active_job, db=db)
        pending_release = _workflow_pending_release_summary(db, disc_record)
    else:
        workflow_short = _workflow_discdb_hit_for_disc_response(active_job, disc_info)
        labelForm = {
            "mode": (disc_info.get("title_type") or "movie").lower(),
            "group_type": (disc_info.get("title_type") or "movie").lower(),
            "disc_group": "",
            "disc_number": None,
            "tmdb_id": "",
            "disc_format": disc_info.get("disc_format"),
            "release_name": "",
            "release_slug": "",
            "info_title": disc_info.get("info_title"),
            "upc": None,
            "asin": None,
            "cover_front_url": None,
            "cover_back_url": None,
            "release_year": disc_info.get("release_year"),
            "production_year": disc_info.get("production_year"),
            "disc_name": "",
            "disc_slug": "",
            "movie_id": disc_info.get("movie_id"),
            "boxset_id": None,
            "workflow_step": "summary" if workflow_short else "film",
            "tracks": [],
        }

    # 5. Build titles using O(N) indexed merge (was O(N²) nested loop).
    # Skip if titles not included (e.g. lightweight card click with include=label,job).
    # Build a source_file → DB title index once, then look up in O(1).
    titles_by_id = {}
    
    # Build source_file index from DB titles for O(1) lookup
    db_titles_by_source: dict = {}
    if disc_record and disc_record.titles:
        for t in disc_record.titles:
            if t.source_file:
                db_titles_by_source[t.source_file] = t
    
    # Extract from disc_info.titles (cache), map to DB title_id via source_file
    if disc_info.get("titles"):
        titles_data = disc_info["titles"]
        items = []
        if isinstance(titles_data, dict):
            items = list(titles_data.items())
        elif isinstance(titles_data, list):
            items = [(t.get("title_id"), t) for t in titles_data if isinstance(t, dict)]
        
        for key, title_data in items:
            if not isinstance(title_data, dict):
                continue
            src = title_data.get("source_file") or title_data.get("src") or title_data.get("file") or title_data.get("track_id")
            if not src:
                if isinstance(key, str) and ("." in key or not key.isdigit()):
                    src = key
                else:
                    continue
            title_data_no_title = {
                k: v for k, v in title_data.items()
                if k not in ("title", "title_id", "title_seq")
            }
            # O(1) lookup instead of O(N) loop
            mapped_title = db_titles_by_source.get(src)
            if mapped_title:
                titles_by_id[str(mapped_title.id)] = {
                    "src": str(mapped_title.id),
                    "source_file": src,
                    "title_id": mapped_title.id,
                    **title_data_no_title,
                }
    
    # Merge titles from database (add any DB titles not already present from cache)
    if disc_record and disc_record.titles:
        for title in disc_record.titles:
            title_id = str(title.id)
            src = title.source_file or f"title_{title_id}"
            if title_id not in titles_by_id:
                # #349: Strip heavy fields from workflow-context titles.
                # metadata_scan (~2KB each) and segment_map are available via
                # GET /discs/{id}/titles?detail=full for on-demand loading.
                meta = getattr(title, "metadata_scan", None)
                titles_by_id[title_id] = {
                    "src": title_id,
                    "source_file": src,
                    "title_id": title.id,
                    # segment_map omitted — use paginated endpoint with detail=full
                    "index": title.index,
                    "order_index": title.order_index,
                    "title": title.title,
                    "description": title.description,
                    "type": _normalize_title_type(title.type),
                    "season": title.season,
                    "episode": title.episode,
                    "part": title.part,
                    "part_of": title.part_of,
                    "episode_end": title.episode_end,
                    "duration": title.duration,
                    "duration_raw": title.duration_raw,
                    "size": title.size,
                    "display_size": title.display_size,
                    "comment": title.comment,
                    "edition": title.edition,
                    "active": title.active,
                    "detection_warning": title.detection_warning,
                    "detection_confidence": title.detection_confidence,
                    # metadata_scan omitted — summary only for lightweight display
                    "metadata_summary": metadata_scan_to_summary(meta) if meta else None,
                    # Source-split for the titles-step chip system (auto_*
                    # from automated detection; user_* from direct user
                    # input; the legacy resolved columns are the cache).
                    **crud.title_provenance_payload(title),
                    "subsumed_by_title_id": getattr(title, "subsumed_by_title_id", None),
                    "obfuscation_flag": bool(getattr(title, "obfuscation_flag", False)),
                    "obfuscation_reason": getattr(title, "obfuscation_reason", None),
                    "force_independent_group": bool(getattr(title, "force_independent_group", False)),
                }
            else:
                # disc_info.titles can carry stale DiscDB-enriched labeling; DiscTitle rows win.
                existing = titles_by_id[title_id]
                existing["title"] = title.title if title.title is not None else existing.get("title")
                if title.description is not None:
                    existing["description"] = title.description
                existing["type"] = _normalize_title_type(title.type)
                # Carry the source-split + subsumption from the DB row.
                existing.update(crud.title_provenance_payload(title))
                existing["subsumed_by_title_id"] = getattr(title, "subsumed_by_title_id", None)
                existing["obfuscation_flag"] = bool(getattr(title, "obfuscation_flag", False))
                existing["obfuscation_reason"] = getattr(title, "obfuscation_reason", None)
                if title.season is not None:
                    existing["season"] = title.season
                if title.episode is not None:
                    existing["episode"] = title.episode
                if title.edition is not None:
                    existing["edition"] = title.edition
                if not existing.get("duration") and title.duration:
                    existing["duration"] = title.duration
                if not existing.get("size") and title.size:
                    existing["size"] = title.size
                # #349: Only set lightweight summary, not full metadata_scan/segment_map
                meta = getattr(title, "metadata_scan", None)
                existing["metadata_summary"] = metadata_scan_to_summary(meta) if meta else None

    # Compute dedupe-group membership for the left-rail collapse. Mirrors
    # what `jobs._build_workflow_context` does for the job-scoped endpoint
    # — without this, `state.context.dedupeGroups` is empty on disc-card
    # mounts and the title list renders every sibling as its own row.
    dedupe_groups: list[dict] = []
    if disc_id:
        attach_duplicate_info(titles_by_id, disc_id)
        try:
            from core.path_b_dedupe import (
                annotate_titles_with_dedupe_group as _annotate_dedupe,
                compute_dedupe_groups as _compute_dedupe,
                compute_mpls_clip_index as _compute_clip_index,
                fold_subsumption_into_groups as _fold_subsumption,
            )
            _groups = _compute_dedupe(titles_by_id)
            # Fold subsumed m2ts into their wrapper's group (parity with the
            # job-scoped builder). Read-only: persistence of the subsumption
            # marks stays on the job context path.
            _groups = _fold_subsumption(
                _groups, _compute_clip_index(titles_by_id), titles_by_id,
            )
            _annotate_dedupe(titles_by_id, _groups)
            dedupe_groups = [g.to_dict() for g in _groups]
        except Exception as _exc:
            log.warning(
                "Disc workflow-context: dedupe-group computation failed for "
                "disc %s: %s", disc_id, _exc,
            )

    # Convert dict to list and build title_order (by index/order if available)
    titles = list(titles_by_id.values())
    titles.sort(key=lambda t: (t.get("order_index") if t.get("order_index") is not None else 9999,
                               t.get("index") if t.get("index") is not None else 9999))
    title_order = [str(t["title_id"]) for t in titles]

    # 6. Options are NOT loaded here - frontend fetches them separately via GET /discs/options

    # 6.5. Get related data (release discs, boxset movies, release details)
    # Note: postProcessFiles and transferDestination are job-specific, so empty for disc contexts
    release_discs = []
    boxset_movies = []
    last_release_details = None

    if include_release and disc_record and disc_record.release:
        rel = disc_record.release
        release_discs = _get_release_discs_for_disc(rel, db)
        last_release_details = _get_release_details_for_disc(rel)

        # Get boxset movies if release is in a boxset
        if rel.boxset_id:
            boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == rel.boxset_id).first()
            if boxset:
                boxset_movies = _get_boxset_movies_for_disc(boxset, db)
    
    # 7. jobStatus when requested (active_job loaded above for workflow branch + prefill)
    jobStatus = None
    if include_job and active_job:
        jobStatus = _job_to_status(active_job)

    # 8. Get job-specific fields when job exists (unified with job contexts)
    post_process_files = []
    transfer_destination = None
    if active_job:
        # Include post-process files and transfer destination from the active job
        post_process_files = _get_post_process_files_from_jobs(active_job)
        transfer_destination = _get_transfer_destination_from_jobs(active_job, db)
    
    # 9. Build discInfo for response (needed for frontend to extract disc_id)
    disc_detail = None
    if include_discinfo:
        disc_info_with_id = disc_info.copy()
        if disc_id:
            disc_info_with_id['disc_id'] = disc_id
        if disc_record:
            crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info_with_id)
            _merge_persisted_tmdb_suggestion(disc_info_with_id, disc_record)
        disc_info_with_id["titles"] = {str(k): v for k, v in titles_by_id.items()}
        disc_detail = _safe_disc_detail(disc_info_with_id, disc_num or "unknown", mount_point or "unknown")
    else:
        # Lightweight discInfo with just disc_id (frontend needs it for routing).
        # ALSO include tmdb_suggestion (#388/#389) — it's a small JSON blob, the
        # frontend needs it to render the film-step suggestion card just like it
        # needs disc_id to route. The frontend's `include=label,job` doesn't
        # signal "I don't want tmdb_suggestion"; it signals "skip the heavy
        # titles/tracks computation". Same intent.
        lightweight = {
            "disc_id": disc_id,
            "disc_num": disc_num or "unknown",
            "mount_point": mount_point or "unknown",
        }
        if disc_record:
            _merge_persisted_tmdb_suggestion(lightweight, disc_record)
        disc_detail = _safe_disc_detail(
            lightweight,
            disc_num or "unknown", mount_point or "unknown",
        )
    
    titles_version = _get_titles_version(disc_record)
    
    # 10. Build and return WorkflowContextResponse
    discdb_metadata_hit = disc_info.get("discdb_hit") is True
    workflow_disc_hit = _workflow_discdb_hit_for_disc_response(active_job, disc_info)
    # For DiscDB metadata match, fall back to disc_info when no release (prefill for UI)
    has_release_cover = disc_record and disc_record.release and disc_record.release.movie
    if has_release_cover:
        # Use release cover when movie has no poster (e.g. DiscDB-created movie with cover on release)
        movie_cover = (
            disc_record.release.movie.cover_url or disc_record.release.cover_front_url
        )
    else:
        movie_cover = (
            disc_info.get("movie_cover_url") or disc_info.get("release_image")
            if discdb_metadata_hit
            else None
        )
    movie_name = (
        disc_record.release.movie.name
        if has_release_cover
        else (disc_record.info_title if disc_record and disc_record.info_title else disc_info.get("info_title"))
    )
    if discdb_metadata_hit and not movie_name:
        movie_name = disc_info.get("movie_name")
    production_year = (
        disc_record.release.movie.production_year
        if has_release_cover
        else None
    )
    if discdb_metadata_hit and production_year is None:
        production_year = disc_info.get("production_year")
    discdb_result_ctx = _workflow_context_discdb_result(active_job, disc_info, disc_id=disc_id, db=db)
    return WorkflowContextResponse(
        id=disc_id or mount_point,
        type="disc",
        discId=disc_id,
        mountPoint=mount_point,
        discNum=disc_num,
        labelForm=labelForm,
        titles=titles,
        titleOrder=title_order,
        titlesVersion=titles_version,
        dedupeGroups=dedupe_groups,
        jobStatus=jobStatus,  # Include jobStatus if active job exists
        discInfo=disc_detail,  # Include discInfo for frontend to extract disc_id
        movieOptions=[],  # Loaded separately via GET /discs/options
        boxsetOptions=[],
        releaseOptions=[],
        pendingRelease=pending_release,
        groupOptions=[],
        labelDraftProcessed=bool(disc_record and disc_record.label_draft),
        discNameLocked=bool(disc_record and disc_record.disc_name),
        discSlugLocked=bool(disc_record and disc_record.disc_slug),
        isSeries=(disc_info.get("title_type") or "").lower() == "series",
        discdbHit=workflow_disc_hit,
        discdb_result=discdb_result_ctx,
        discMode="copy",  # Default, can be overridden
        lastReleaseDetails=last_release_details,
        releaseNameHint=disc_info.get("movie_name") or "",
        releaseSlugHint="",
        postProcessFiles=post_process_files,  # Include job-specific fields when job exists
        transferDestination=transfer_destination,  # Include job-specific fields when job exists
        releaseDiscs=release_discs,
        boxsetMovies=boxset_movies,
        movieCover=movie_cover,
        movieName=movie_name,
        productionYear=production_year,
    )


def _determine_disc_state(disc_id: str) -> str:
    """
    Determine disc_state for a disc.
    
    Returns:
        'in_drive' if disc is currently inserted (in cache)
        'unfinished' if disc is ejected but has active job
    """
    try:
        cached_discs = get_cached_discs()
        for cached_disc in cached_discs:
            if cached_disc.get("disc_id") == disc_id:
                return 'in_drive'
    except Exception:
        pass
    return 'unfinished'


@router.get("/{disc_id}/workflow-context", response_model=WorkflowContextResponse)
def get_disc_workflow_context_by_id(
    disc_id: str,
    db: Session = Depends(database.get_db),
    include: str = Query("label,titles,job,release,discinfo", description="Comma-separated fields to include: label,titles,job,release,discinfo"),
):
    """
    Get workflow context for a disc using disc_id (after hashing).
    This is the preferred method once disc_id is available.
    
    Note: Sync endpoint (def, not async def) so FastAPI runs it in a threadpool,
    preventing event loop blocking during synchronous DB operations.
    """
    # Check in-memory cache first (avoids DB queries for rapid refetches)
    cached = _get_cached_context(f"disc:{disc_id}")
    if cached is not None:
        return cached

    # Parse include parameter for deferred loading
    # When called directly (not via HTTP), include may be a Query object — default to full
    include_str = include if isinstance(include, str) else "label,titles,job,release,discinfo"
    include_set = set(s.strip() for s in include_str.split(",")) if include_str else {"label", "titles", "job", "release", "discinfo"}
    include_titles = "titles" in include_set
    include_release = "release" in include_set
    include_discinfo = "discinfo" in include_set
    include_job = "job" in include_set
    
    # 1. Load disc record by disc_id
    # Only eager-load titles if included (skip for lightweight card click)
    # Use selectinload for titles — joinedload combined with the release+movie+
    # boxset joins produces a Cartesian-product result row count and causes
    # psycopg2 to JSON-decode all 200+ title rows for every workflow-context
    # call on Midway-class discs (~30s+ per call). selectinload fires a
    # separate IN-query that's ~constant time regardless of join breadth.
    load_options = [
        joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
        joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
    ]
    if include_titles:
        load_options.append(selectinload(db_models.Disc.titles))
    disc_record = (
        db.query(db_models.Disc)
        .options(*load_options)
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    try:
        titles_sample = []
        for t in (disc_record.titles or [])[:3] if disc_record else []:
            titles_sample.append({"id": str(getattr(t, "id", None)), "source_file": getattr(t, "source_file", None)})
    except Exception:
        pass
    
    if not disc_record:
        raise HTTPException(404, detail=f"Disc with id {disc_id} not found")
    
    # 2. Try to get disc info from cache if disc is currently inserted
    # If not in cache, build context from database record (for ejected discs or historical contexts)
    # Note: Disc model doesn't store mount_point - it's only available when disc is inserted (from cache)
    disc_info = None
    mount_point = None  # Will be set from cache if disc is currently inserted
    disc_num = None  # Will be determined from cache or remain None for ejected discs
    
    # Try to get from cache first (if disc is currently inserted)
    try:
        currently_inserted_discs = get_cached_discs()
        
        # Find the disc that matches our disc_id
        matching_disc = None
        for cached_disc in currently_inserted_discs:
            # Check if disc_id matches
            if cached_disc.get("disc_id") == disc_id:
                matching_disc = cached_disc
                break
            # Fallback: check by content_hash if disc_id not available in cache
            elif cached_disc.get("disc_hash") == disc_record.content_hash:
                matching_disc = cached_disc
                break
        
        if matching_disc:
            # Disc is currently inserted - use cached info
            disc_info = matching_disc.copy()
            mount_point = matching_disc.get("mount_point") or mount_point
            disc_num = matching_disc.get("disc_num") or disc_num
            disc_info.setdefault("disc_num", disc_num)
            disc_info.setdefault("mount_point", mount_point)
    except Exception as exc:
        log.warning(f"Failed to get cached discs for workflow context (disc may not be inserted): {exc}")
        # Continue with database record - disc may be ejected or not yet scanned
    
    # If not in cache, build disc_info from database record
    if not disc_info:
        # Build minimal disc_info from database record
        disc_info = {
            "disc_id": str(disc_record.id),
            "disc_num": disc_num or "unknown",
            "mount_point": mount_point or "unknown",
            "disc_hash": disc_record.content_hash,
            "disc_name": disc_record.disc_name,
            "disc_slug": disc_record.disc_slug,
            "disc_format": disc_record.format,
            "pending": not mount_point,  # Mark as pending if no mount_point
        }
        
        # Try to get additional info from cache using disc_num or content_hash
        cached = None
        if disc_num and disc_num != "unknown":
            try:
                cached = cache_get(str(disc_num))
            except Exception:
                pass  # Ignore cache lookup errors
        
        # If not found by disc_num, try by content_hash
        if not cached and disc_record.content_hash:
            try:
                cached = cache_get(str(disc_record.content_hash))
            except Exception:
                pass  # Ignore cache lookup errors
        
        if cached:
            # Merge cached data (may have more complete info)
            disc_info.update(cached)
            if disc_num:
                disc_info["disc_num"] = disc_num
            elif cached.get("disc_num"):
                disc_info["disc_num"] = cached.get("disc_num")
            disc_info["mount_point"] = mount_point or cached.get("mount_point") or "unknown"

    crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
    # Surface persisted TMDB auto-suggestion (#388) so the film step (#389)
    # can render the suggestion card on an already-scanned, ejected disc.
    _merge_persisted_tmdb_suggestion(disc_info, disc_record)

    active_job = None
    try:
        active_job = (
            db.query(db_models.Job)
            .filter(db_models.Job.disc_id == disc_id)
            .filter(db_models.Job.job_status.in_(["pending", "running", "validating"]))
            .order_by(db_models.Job.created_at.desc())
            .first()
        )
    except Exception as exc:
        log.warning(f"Failed to get active job for disc {disc_id} (disc workflow context): {exc}")

    # 3. Build labelForm from disc record and disc info
    labelForm = _build_labelform_from_disc(disc_record, disc_info, active_job=active_job, db=db)
    pending_release = _workflow_pending_release_summary(db, disc_record)

    # 4. Extract titles from disc_info.titles and disc_record.titles, merging both sources
    # PRIORITY: Database records (disc_record.titles) are authoritative for user-edited fields
    # Cache (disc_info.titles) is used only for initial data or missing fields
    titles = []
    title_order = []
    titles_by_id = {}  # Use dict to merge and deduplicate by title_id
    titles_by_src = {}
    
    # First, extract from disc_info.titles (for initial/fallback data)
    disc_info_titles_count = 0
    disc_info_titles_type = type(disc_info.get("titles")).__name__ if disc_info.get("titles") else "None"
    if disc_info.get("titles"):
        titles_data = disc_info["titles"]
        # Handle both dict and list formats
        if isinstance(titles_data, dict):
            # Dict format: could be {source_file: {title_data}} OR {index: {title_data}}
            for key, title_data in titles_data.items():
                if isinstance(title_data, dict):
                    # Extract source_file from title_data itself (not from dict key)
                    # The key might be numeric index or source_file string
                    src = title_data.get("source_file") or title_data.get("src") or title_data.get("file") or title_data.get("track_id") or title_data.get("title_id")
                    # If title_data doesn't have source_file, try using the key if it looks like a source_file
                    if not src:
                        # Check if key is a source_file string (contains .mpls, .m2ts, etc.) vs numeric index
                        if isinstance(key, str) and ("." in key or not key.isdigit()):
                            src = key
                        else:
                            # Skip titles without source_file - can't match them
                            continue
                    # Don't use title field from cache - only use database titles for the title field
                    # Cache may have title set to info_title or other incorrect values
                    # Exclude title field from cache data - database will provide it if available
                    title_data_no_title = {k: v for k, v in title_data.items() if k != "title"}
                    
                    titles_by_src[src] = title_data_no_title
                    disc_info_titles_count += 1
        elif isinstance(titles_data, list):
            # List format: [{source_file: "...", ...}, ...]
            for title_data in titles_data:
                if isinstance(title_data, dict):
                    # Extract source_file from the title data itself
                    src = title_data.get("source_file") or title_data.get("src") or title_data.get("file") or title_data.get("track_id") or title_data.get("title_id")
                    if src:
                        # Don't use title field from cache - only use database titles for the title field
                        # Cache may have title set to info_title or other incorrect values
                        # Exclude title field from cache data - database will provide it if available
                        title_data_no_title = {k: v for k, v in title_data.items() if k != "title"}
                        
                        titles_by_src[src] = title_data_no_title
                        disc_info_titles_count += 1
    
    # Then, PRIORITIZE disc_record.titles (database records) - these have user edits
    db_titles_count = 0
    db_titles_matched = 0
    db_titles_added = 0
    if disc_record.titles:
        for title in disc_record.titles:
            title_id = str(title.id)
            src = title.source_file or f"title_{title.id}"
            db_titles_count += 1
            # Database records override cache - user edits are saved here
            if src in titles_by_src:
                db_titles_matched += 1
                cache_fields = titles_by_src[src]
                titles_by_id[title_id] = {
                    **cache_fields,
                    "src": title_id,
                    "source_file": src,
                    "title_id": title.id,
                    "title_seq": title.title_seq,
                }
            else:
                titles_by_id[title_id] = {
                    "src": title_id,
                    "source_file": src,
                    "title_id": title.id,
                    "title_seq": title.title_seq,
                }
            # Override user-editable fields from database (title, description, type, etc.)
            existing = titles_by_id[title_id]
            existing["title"] = title.title if title.title is not None else existing.get("title", "")
            if title.description is not None:
                existing["description"] = title.description
            if title.type is not None:
                existing["type"] = _normalize_title_type(title.type)
            if title.season is not None:
                existing["season"] = title.season
            if title.episode is not None:
                existing["episode"] = title.episode
            if title.comment is not None:
                existing["comment"] = title.comment
            # Update metadata fields if available
            if title.duration is not None:
                existing["duration"] = title.duration
            if title.duration_raw is not None:
                existing["duration_raw"] = title.duration_raw
            if title.size is not None:
                existing["size"] = title.size
            if title.display_size is not None:
                existing["display_size"] = title.display_size
            if title.index is not None:
                existing["index"] = title.index
            if title.order_index is not None:
                existing["order_index"] = title.order_index
            # Preserve chapters from database (important metadata that can be lost)
            if title.chapters is not None:
                existing["chapters"] = title.chapters
            # Preserve streams from database
            if title.streams is not None:
                existing["streams"] = title.streams
            # FFprobe metadata scan and summary (for UI "what's inside?" hover)
            meta = getattr(title, "metadata_scan", None)
            existing["metadata_scan"] = meta
            existing["metadata_summary"] = metadata_scan_to_summary(meta) if meta else None
            existing["segment_map"] = getattr(title, "segment_map", None)
            # Source-split + subsumption for the titles-step chip system.
            existing.update(crud.title_provenance_payload(title))
            existing["subsumed_by_title_id"] = getattr(title, "subsumed_by_title_id", None)
            existing["obfuscation_flag"] = bool(getattr(title, "obfuscation_flag", False))
            existing["obfuscation_reason"] = getattr(title, "obfuscation_reason", None)
            existing["force_independent_group"] = bool(getattr(title, "force_independent_group", False))
            # Add new title from database (not in cache)
            if src not in titles_by_src:
                db_titles_added += 1
                meta = getattr(title, "metadata_scan", None)
                titles_by_id[title_id] = {
                    "src": title_id,
                    "source_file": src,
                    "title_id": title.id,
                    "title_seq": title.title_seq,
                    "segment_map": getattr(title, "segment_map", None),
                    "title": title.title,
                    "description": title.description,
                    "type": _normalize_title_type(title.type),
                    "season": title.season,
                    "episode": title.episode,
                    "part": title.part,
                    "part_of": title.part_of,
                    "episode_end": title.episode_end,
                    "duration": title.duration,
                    "duration_raw": title.duration_raw,
                    "size": title.size,
                    "display_size": title.display_size,
                    "comment": title.comment,
                    "chapters": title.chapters,  # Preserve chapters metadata
                    "streams": title.streams,  # Preserve streams metadata
                    "index": title.index,
                    "order_index": title.order_index,
                    "metadata_scan": meta,
                    "metadata_summary": metadata_scan_to_summary(meta) if meta else None,
                    **crud.title_provenance_payload(title),
                    "subsumed_by_title_id": getattr(title, "subsumed_by_title_id", None),
                    "obfuscation_flag": bool(getattr(title, "obfuscation_flag", False)),
                    "obfuscation_reason": getattr(title, "obfuscation_reason", None),
                }

            # This branch REBUILDS the dict, discarding everything assigned to
            # `existing` above — so anything the grouping pass reads has to be
            # re-stated here. force_independent_group was missing, which made
            # this endpoint report a title as still grouped after Ungroup
            # (the job-scoped builder and the DB both said otherwise), so the
            # client refetched and saw no change (mkv-auto-release#8).
            titles_by_id[title_id]["force_independent_group"] = bool(
                getattr(title, "force_independent_group", False)
            )

    # Compute dedupe-group membership for the left-rail collapse (parity
    # with the mount-keyed endpoint above). Without this the disc-id
    # endpoint returns an empty dedupeGroups[] and the frontend can't
    # collapse permutation siblings into one row.
    dedupe_groups: list[dict] = []
    if disc_id:
        attach_duplicate_info(titles_by_id, disc_id)
        try:
            from core.path_b_dedupe import (
                annotate_titles_with_dedupe_group as _annotate_dedupe,
                compute_dedupe_groups as _compute_dedupe,
                compute_mpls_clip_index as _compute_clip_index,
                fold_subsumption_into_groups as _fold_subsumption,
            )
            _groups = _compute_dedupe(titles_by_id)
            # Fold subsumed m2ts into their wrapper's group (parity with the
            # job-scoped builder). Read-only: persistence of the subsumption
            # marks stays on the job context path.
            _groups = _fold_subsumption(
                _groups, _compute_clip_index(titles_by_id), titles_by_id,
            )
            _annotate_dedupe(titles_by_id, _groups)
            dedupe_groups = [g.to_dict() for g in _groups]
        except Exception as _exc:
            log.warning(
                "Disc-id workflow-context: dedupe-group computation failed "
                "for disc %s: %s", disc_id, _exc,
            )

    # Convert dict to list and build title_order
    titles = list(titles_by_id.values())
    titles.sort(key=lambda t: (t.get("order_index") if t.get("order_index") is not None else 9999,
                               t.get("index") if t.get("index") is not None else 9999))
    title_order = [str(t["title_id"]) for t in titles if t.get("title_id")]
    try:
        sample_ids = [str(t.get("title_id")) for t in titles[:3]]
    except Exception:
        pass
    
    # 5. Options are NOT loaded here - frontend fetches them separately via GET /discs/options
    
    # 5.5. Get related data (release discs, boxset movies, release details)
    # Skip if not included (deferred loading — only load when user navigates to disc/boxset step)
    release_discs = []
    boxset_movies = []
    last_release_details = None
    
    if include_release and disc_record.release:
        rel = disc_record.release
        release_discs = _get_release_discs_for_disc(rel, db)
        last_release_details = _get_release_details_for_disc(rel)

        if rel.boxset_id:
            boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == rel.boxset_id).first()
            if boxset:
                boxset_movies = _get_boxset_movies_for_disc(boxset, db)

    # 6. jobStatus when requested (active_job loaded above for workflow branch)
    jobStatus = None
    if include_job and active_job:
        jobStatus = _job_to_status(active_job)

    # 7. Get job-specific fields when job exists (unified with job contexts)
    post_process_files = []
    transfer_destination = None
    if active_job:
        # Include post-process files and transfer destination from the active job
        post_process_files = _get_post_process_files_from_jobs(active_job)
        transfer_destination = _get_transfer_destination_from_jobs(active_job, db)
    
    # 8. Build discInfo for response (needed for frontend to extract disc_id)
    # Ensure disc_id is in disc_info dict; use merged titles (with metadata_scan/summary)
    disc_info_with_id = disc_info.copy()
    disc_info_with_id['disc_id'] = disc_id
    crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info_with_id)
    disc_info_with_id["titles"] = {str(k): v for k, v in titles_by_id.items()}
    disc_detail = _safe_disc_detail(disc_info_with_id, disc_num or "unknown", mount_point or "unknown")

    titles_version = _get_titles_version(disc_record)

    # 9. Build WorkflowContextResponse, cache, and return
    workflow_disc_hit = _workflow_discdb_hit_for_disc_response(active_job, disc_info)
    discdb_result_ctx = _workflow_context_discdb_result(active_job, disc_info, disc_id=disc_id, db=db)

    response = WorkflowContextResponse(
        id=disc_id,
        type="disc",
        discId=disc_id,
        mountPoint=mount_point,
        discNum=disc_num,
        labelForm=labelForm,
        titles=titles,
        titleOrder=title_order,
        titlesVersion=titles_version,
        dedupeGroups=dedupe_groups,
        jobStatus=jobStatus,
        discInfo=disc_detail,  # Include discInfo for frontend to extract disc_id
        movieOptions=[],  # Loaded separately via GET /discs/options
        boxsetOptions=[],
        releaseOptions=[],
        pendingRelease=pending_release,
        groupOptions=[],
        labelDraftProcessed=bool(disc_record.label_draft),
        discNameLocked=bool(disc_record.disc_name),
        discSlugLocked=bool(disc_record.disc_slug),
        isSeries=(disc_info.get("title_type") or "").lower() == "series",
        discdbHit=workflow_disc_hit,
        discdb_result=discdb_result_ctx,
        discMode="copy",  # Default, can be overridden
        lastReleaseDetails=last_release_details,
        releaseNameHint=disc_info.get("movie_name") or "",
        releaseSlugHint="",
        postProcessFiles=post_process_files,  # Include job-specific fields when job exists
        transferDestination=transfer_destination,  # Include job-specific fields when job exists
        releaseDiscs=release_discs,
        boxsetMovies=boxset_movies,
        # Use release cover when movie has no poster (e.g. DiscDB-created movie)
        movieCover=(
            (disc_record.release.movie.cover_url or disc_record.release.cover_front_url)
            if disc_record.release and disc_record.release.movie
            else (disc_record.release.cover_front_url if disc_record.release else None)
        ),
        movieName=(
            disc_record.release.movie.name
            if disc_record.release and disc_record.release.movie
            else (disc_record.info_title if disc_record.info_title else disc_info.get("info_title"))
        ),
        productionYear=(
            disc_record.release.movie.production_year
            if disc_record.release and disc_record.release.movie
            else None
        ),
    )
    
    _set_cached_context(f"disc:{disc_id}", response)
    return response


@router.put("/workflow-context", response_model=WorkflowContextResponse)
def save_disc_workflow_context_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Save complete labelForm for a disc using mount_point.
    Converts labelForm to ops and calls patch_disc_ops_internal.
    
    Architecture: Reads from Disc Manager cache only. Drive Manager handles disc detection
    and scanning, then notifies Disc Manager to cache the data.
    """
    mount_point = _normalize_mount_point(mount_point)
    # 1. Find disc by mount_point from cache (no scan)
    cached_discs = get_cached_discs()
    
    disc_match = None
    for disc_info in cached_discs:
        if disc_info.get("mount_point") == mount_point:
            disc_match = disc_info
            break
    
    if not disc_match:
        raise HTTPException(404, detail=f"Disc with mount_point {mount_point} not found in cache")
    
    # Use cached disc info (no scan triggered)
    disc_info = disc_match
    disc_hash = disc_info.get("disc_hash")
    if not disc_hash:
        raise HTTPException(400, detail="Disc has not been hashed yet")
    
    # Find or create disc record to get disc_id (defer disc→release until metadata link-ready)
    disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, disc_info)
    crud.merge_pending_release_into_disc_info_dict(disc_record, disc_info)
    disc_id = str(disc_record.id)
    
    # 2. Convert labelForm to ops format
    ops = _labelform_to_ops(update.labelForm, is_partial=False)
    
    # 3. Call internal ops function
    from api.routers.releases import _patch_disc_ops_internal
    updated_disc = _patch_disc_ops_internal(disc_id, ops, db)
    db.commit()

    # 4. Return updated workflow context
    return get_disc_workflow_context_by_id(str(updated_disc.id), db)


@router.patch("/workflow-context", response_model=WorkflowContextResponse)
def update_disc_workflow_context_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Partially update labelForm for a disc using mount_point (for auto-save).
    """
    mount_point = _normalize_mount_point(mount_point)
    # Similar to PUT but is_partial=True
    # For now, treat PATCH same as PUT (full update)
    # TODO: Implement true partial update logic if needed
    return save_disc_workflow_context_by_mount(mount_point, update, db)


@router.put("/{disc_id}/workflow-context", response_model=WorkflowContextResponse)
def save_disc_workflow_context_by_id(
    disc_id: str,
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Save complete labelForm for a disc using disc_id.
    """
    # Convert to ops and call internal function
    ops = _labelform_to_ops(update.labelForm, is_partial=False)
    from api.routers.releases import _patch_disc_ops_internal
    updated_disc = _patch_disc_ops_internal(disc_id, ops, db)
    db.commit()
    # Invalidate cached context before rebuilding
    invalidate_workflow_context_cache(disc_id=disc_id)
    # Refresh the disc to ensure we have the latest titles from database
    # Force SQLAlchemy to expire and reload the titles relationship to ensure we get fresh data
    db.expire(updated_disc, ['titles'])
    db.refresh(updated_disc, ['titles'])
    context = get_disc_workflow_context_by_id(str(updated_disc.id), db)
    
    # Emit websocket notification after successful save
    try:
        from api.routers.websockets import _emit_to_disc_workflow
        asyncio.create_task(_emit_to_disc_workflow(str(updated_disc.id), changed_fields=['labelForm']))
    except Exception as exc:
        log.warning(f"Failed to emit workflow context change notification to websocket for disc {disc_id}: {exc}")
    
    return context


@router.patch("/{disc_id}/workflow-context", response_model=WorkflowContextResponse)
def update_disc_workflow_context_by_id(
    disc_id: str,
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Partially update labelForm for a disc using disc_id (for auto-save).
    """
    # For now, treat PATCH same as PUT (full update)
    # TODO: Implement true partial update logic if needed
    return save_disc_workflow_context_by_id(disc_id, update, db)


@router.get("/{disc_id}/titles")
def get_disc_titles(
    disc_id: str,
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    limit: int = Query(default=50, ge=1, le=500, description="Page size (max 500)"),
    detail: str = Query(default="summary", description="'summary' (lightweight) or 'full' (includes metadata_scan, detection)"),
    db: Session = Depends(database.get_db),
):
    """
    Paginated title list for a disc (#349 Phase 2).

    Returns lightweight title summaries by default. Use detail=full for
    metadata_scan, segment_map, detection_flags (expensive for 300+ titles).

    Supports lazy loading: frontend fetches page 0 on titles step entry,
    then loads more as user scrolls.
    """
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(404, detail="Disc not found")

    q = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .order_by(
            db_models.DiscTitle.order_index.asc().nullslast(),
            db_models.DiscTitle.index.asc().nullslast(),
            db_models.DiscTitle.created_at.asc(),
        )
    )
    total = q.count()
    titles = q.offset(offset).limit(limit).all()

    include_full = detail.strip().lower() == "full"

    items = []
    for t in titles:
        item: dict = {
            "title_id": str(t.id),
            "index": t.index,
            "order_index": t.order_index,
            "title": t.title,
            "type": t.type,
            "source_file": t.source_file,
            "duration": t.duration,
            "duration_raw": t.duration_raw,
            "size": t.size,
            "display_size": t.display_size,
            "mkv_size": t.mkv_size,
            "season": t.season,
            "episode": t.episode,
            "part": t.part,
            "part_of": t.part_of,
            "episode_end": t.episode_end,
            "edition": t.edition,
            "description": t.description,
            "comment": t.comment,
            "active": t.active,
            "detection_warning": t.detection_warning,
            "detection_confidence": t.detection_confidence,
            # #383: ship title_seq so the frontend can include it in title
            # PATCHes and detect stale_seq conflicts from concurrent edits.
            "title_seq": getattr(t, "title_seq", 0) or 0,
        }
        if include_full:
            item["metadata_scan"] = getattr(t, "metadata_scan", None)
            item["segment_map"] = getattr(t, "segment_map", None)
            item["detection_flags"] = getattr(t, "detection_flags", None)
            item["source_hash"] = t.source_hash
            item["output_hash"] = t.output_hash
            item["file_path"] = t.file_path
            item["file_path_stage"] = t.file_path_stage
        items.append(item)

    return {
        "items": items,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < total,
    }


def get_active_job_for_disc(db: Session, disc_id: str):
    """Most recent in-flight job for a disc, or None. Completed/failed jobs
    don't lock title edits — those are governed by finalize state."""
    return (
        db.query(db_models.Job)
        .filter(
            db_models.Job.disc_id == disc_id,
            db_models.Job.job_status.in_(["pending", "running", "validating"]),
        )
        .order_by(db_models.Job.created_at.desc())
        .first()
    )


def assert_title_patch_allowed(job, title, field_names) -> None:
    """#363 Horizon 1 — title-edit pipeline guard.

    Two locks, mirroring what the pipeline can actually still honor:

    1. ``labels_locked``: once the active job's postprocess or transfer
       has actually consumed the labels, any title edit would diverge DB
       state from the files already renamed/moved. Postprocess is NOT
       auto-dispatched off complete_label — it waits for the user's
       "Start Transfer" click, so the window between complete_label and
       that click is quiescent and safe for edits. label_state=='completed'
       alone therefore does NOT lock; only post_state ∈ (running,
       completed) or transfer_state ∈ (running, completed) does.
       Relabel-after-postprocess is Horizon 2+ (#363).
    2. ``type_change_locked``: un-ignoring a title after a *selective*
       rip started can reference a file MakeMKV never produced. Full
       (all-mode) rips produce every title, so un-ignore stays allowed
       there — that's the main mistake-fixing flow this issue exists for.
    """
    if job is None or not field_names:
        return
    label_state = getattr(job, "label_state", None)
    post_state = job.derived_post_state
    transfer_state = getattr(job, "transfer_state", None)
    if post_state in ("running", "completed") or transfer_state in ("running", "completed"):
        raise HTTPException(
            409,
            detail={
                "error_code": "labels_locked",
                "message": (
                    "Labels were already consumed by post-processing; "
                    "editing titles mid-pipeline isn't supported yet"
                ),
                "label_state": label_state,
                "post_state": post_state,
                "transfer_state": transfer_state,
            },
        )
    if "type" not in field_names:
        return
    rip_set = getattr(job, "rip_set", None)
    rip_state = getattr(job, "rip_state", None)
    currently_ignored = (str(getattr(title, "type", "") or "").strip().lower() == "ignore")
    if (
        currently_ignored
        and isinstance(rip_set, list)
        and rip_set
        and rip_state in ("running", "completed")
    ):
        # If this title's MakeMKV index was part of the selective rip its
        # file exists on disk, so un-ignoring it is still safe.
        title_index = getattr(title, "index", None)
        try:
            in_rip_set = title_index is not None and int(title_index) in {int(x) for x in rip_set}
        except (TypeError, ValueError):
            in_rip_set = False
        if not in_rip_set:
            raise HTTPException(
                409,
                detail={
                    "error_code": "type_change_locked",
                    "message": (
                        "This title was excluded from the selective rip; it has no "
                        "ripped file, so it can't be un-ignored after copy started"
                    ),
                    "rip_state": rip_state,
                },
            )




def _stamp_user_edit(disc) -> None:
    """Record that a human changed this disc's metadata.

    Called only from the user-facing PATCH endpoints — pipeline writes must not
    reach this, or DiscDB dirty-detection would flag every processed disc. A
    DiscDB hit with this stamp is exported as an *update* (#741).
    """
    from datetime import datetime, timezone

    disc.user_edited_at = datetime.now(timezone.utc)


# The fields a human actually CORRECTS — the ones that make a DiscDB hit worth
# re-exporting as an update. Filenames (comment), durations, sizes, stream data
# and ordering are environment/technical values: they differ between two rips
# of the same disc without upstream being wrong, so they must not mark the
# disc dirty.
_USER_CORRECTION_FIELDS = ("title", "type", "season", "episode", "description", "edition")


def _changes_user_surface(title, fields: Dict[str, Any]) -> bool:
    """True when this patch changes a value on a user-correction surface.

    A patch that only touches technical fields — or that re-saves the same
    values — is not a correction, and must not flag the disc for a
    TheDiscDB update export.
    """
    def norm(key: str, value: Any) -> Any:
        if key == "type":
            try:
                value = _normalize_title_type(value)
            except Exception:  # noqa: BLE001 - comparison must never block a patch
                pass
        if isinstance(value, str) and value.strip() == "":
            return None
        return value

    for key in _USER_CORRECTION_FIELDS:
        if key not in fields:
            continue
        if norm(key, fields[key]) != norm(key, getattr(title, key, None)):
            return True
    return False



def _resolve_patch_seq(patch, title) -> "tuple[int | None, int]":
    """Decide the version this patch writes. Returns (new_seq, current_seq).

    ``new_seq is None`` means the write is stale and must be rejected.

    Stage 2 of the title-state redesign (#778). Two protocols:

    - ``base_seq`` (preferred): the version the client READ. The server
      compares it to the row and assigns ``current + 1`` itself. The client
      never computes a version, so it cannot guess wrong — which is what
      turned unobserved background writes into bogus "conflicts".
    - ``title_seq`` (legacy): the version the client claims to write,
      computed client-side as cached+1. Kept working for older clients.

    Absent both, the write is unconditional (current + 1) — unchanged
    behavior for callers that never versioned.
    """
    current_seq = getattr(title, "title_seq", 0) or 0
    base_seq = getattr(patch, "base_seq", None)
    if base_seq is not None:
        # If-Match: anything other than an exact match means the row moved
        # under us. Equal-or-newer is not "close enough" — a client holding
        # a newer version than the server has read something we did not write.
        if int(base_seq) != current_seq:
            return None, current_seq
        return current_seq + 1, current_seq
    incoming_seq = getattr(patch, "title_seq", None)
    if incoming_seq is None:
        return current_seq + 1, current_seq
    if int(incoming_seq) < current_seq:
        return None, current_seq
    return int(incoming_seq), current_seq

@router.patch("/{disc_id}/titles", response_model=TitlePatchResponse)
def patch_disc_title(
    disc_id: str,
    patch: TitlePatchRequest = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Patch a single title by title_id. Only provided fields are updated.
    """
    from api.routers.releases import _ensure_not_finalized
    disc = (
        db.query(db_models.Disc)
        .options(joinedload(db_models.Disc.release))
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    release = getattr(disc, "release", None)
    _ensure_not_finalized(disc, "Disc")
    if release:
        _ensure_not_finalized(release, "Release")

    # with_for_update(): the If-Match check below is read-then-write, and
    # without a row lock two concurrent PATCHes both read the same
    # title_seq, both "match", and both commit the same new seq — measured
    # 7/7 on rc.3 (name-flush + type-change land in the same tick). A
    # collided seq is worse than a lost write: two different row states
    # share one version, so no client merge rule can ever repair it.
    # The lock serializes the pair; the loser now sees the winner's seq
    # and gets an honest stale_seq + current_title to reconcile with.
    title = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.id == patch.title_id, db_models.DiscTitle.disc_id == disc_id)
        .with_for_update()
        .first()
    )
    current_version = _get_titles_version(disc)
    if not title:
        return TitlePatchResponse(
            titles_version=current_version,
            result=TitlePatchResult(
                title_id=patch.title_id,
                success=False,
                error="Title not found",
                error_code="not_found",
            ),
        )

    fields = patch.model_dump(exclude_unset=True)
    fields.pop("title_id", None)
    fields.pop("title_seq", None)
    fields.pop("base_seq", None)
    incoming_seq, current_seq = _resolve_patch_seq(patch, title)
    if not fields:
        return TitlePatchResponse(
            titles_version=current_version,
            result=TitlePatchResult(
                title_id=patch.title_id,
                success=False,
                error="No fields provided to update",
                error_code="no_fields",
            ),
        )

    if incoming_seq is None:
        # Hand back the row as it actually is, so the client reconciles this
        # one row in place rather than refetching the whole disc (#775/#778).
        return TitlePatchResponse(
            titles_version=current_version,
            result=TitlePatchResult(
                title_id=patch.title_id,
                success=False,
                error="Stale title update",
                error_code="stale_seq",
                current_title=_serialize_disc_title(title),
            ),
        )

    assert_title_patch_allowed(get_active_job_for_disc(db, disc_id), title, set(fields))

    corrects_user_surface = _changes_user_surface(title, fields)
    _apply_title_patch_fields(title, fields)

    # Area 2 of the title-state redesign: the duplicate-group sweep runs
    # only on GROUP-SHAPING writes. `type` shapes groups (ignore status
    # feeds demotion/consensus); text fields never do — so a rename or
    # description edit is one row write, no disc-wide sweep, no sibling
    # seq churn, no synced_titles payload. The sweep still runs at scan
    # ingest, label save/complete, and set-primary, which is where group
    # shape actually changes.
    synced_rows: list = []
    if "type" in fields:
        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

        # Per-patch: do NOT consensus-fill NULL primary types from sibling 'ignore' state.
        # A user toggling unignore on a primary clears type to NULL; if every sibling is still
        # 'ignore', consensus-fill would immediately revert the user's change. Bulk paths
        # (label save/complete, scan) keep the default fill_null_type_from_consensus=True.
        # Collect what the sync touches: it bumps siblings' title_seq, and a client
        # that isn't told holds a stale seq cache — its next edit to that sibling
        # is then rejected as a conflict and its recovery wipes the form (#775).
        sync_duplicate_group_labels_for_disc(
            db, disc_id, fill_null_type_from_consensus=False,
            collect_modified=synced_rows,
        )
    title.title_seq = incoming_seq
    new_version = max(_get_titles_version(disc), current_version + 1)
    _set_titles_version(disc, new_version)
    if corrects_user_surface:
        _stamp_user_edit(disc)
    db.commit()

    updated_payload = _serialize_disc_title(title)
    synced_payloads = [
        _serialize_disc_title(t) for t in synced_rows
        if str(t.id) != str(title.id)
    ]
    # Area 4: fan the changed rows out as a delta so other tabs converge
    # without a ~1MB context refetch. Same serialized dicts as the
    # response; per-row seq gating makes the writer's own echo a no-op.
    _emit_titles_changed_threadsafe(disc_id, [updated_payload, *synced_payloads], new_version)

    return TitlePatchResponse(
        titles_version=new_version,
        result=TitlePatchResult(
            title_id=patch.title_id,
            success=True,
            updated_title=updated_payload,
        ),
        synced_titles=synced_payloads or None,
    )


@router.patch("/{disc_id}/titles/batch", response_model=TitlePatchBatchResponse)
def patch_disc_titles_batch(
    disc_id: str,
    batch: TitlePatchBatchRequest = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Patch multiple titles by title_id. Returns per-title results.
    """
    from api.routers.releases import _ensure_not_finalized
    disc = (
        db.query(db_models.Disc)
        .options(joinedload(db_models.Disc.release))
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    release = getattr(disc, "release", None)
    _ensure_not_finalized(disc, "Disc")
    if release:
        _ensure_not_finalized(release, "Release")

    current_version = _get_titles_version(disc)
    results: List[Dict[str, Any]] = []
    any_success = False
    any_correction = False
    any_type_change = False
    synced_rows: list = []
    active_job = get_active_job_for_disc(db, disc_id)

    for patch in batch.patches:
        try:
            # Same row lock as the single-title endpoint: the seq check is
            # read-then-write and must be serialized against concurrent
            # writers (see patch_disc_title).
            title = (
                db.query(db_models.DiscTitle)
                .filter(db_models.DiscTitle.id == patch.title_id, db_models.DiscTitle.disc_id == disc_id)
                .with_for_update()
                .first()
            )
            if not title:
                results.append({
                    "title_id": patch.title_id,
                    "success": False,
                    "error": "Title not found",
                    "error_code": "not_found",
                })
                continue

            fields = patch.model_dump(exclude_unset=True)
            fields.pop("title_id", None)
            fields.pop("title_seq", None)
            fields.pop("base_seq", None)
            incoming_seq, current_seq = _resolve_patch_seq(patch, title)
            if not fields:
                results.append({
                    "title_id": patch.title_id,
                    "success": False,
                    "error": "No fields provided to update",
                    "error_code": "no_fields",
                })
                continue

            if incoming_seq is None:
                results.append({
                    "title_id": patch.title_id,
                    "success": False,
                    "error": "Stale title update",
                    "error_code": "stale_seq",
                    "current_title": _serialize_disc_title(title),
                })
                continue

            try:
                assert_title_patch_allowed(active_job, title, set(fields))
            except HTTPException as guard_exc:
                detail = guard_exc.detail if isinstance(guard_exc.detail, dict) else {}
                results.append({
                    "title_id": patch.title_id,
                    "success": False,
                    "error": detail.get("message") or "Title edit not allowed at this pipeline stage",
                    "error_code": detail.get("error_code") or "patch_locked",
                })
                continue

            if _changes_user_surface(title, fields):
                any_correction = True
            if "type" in fields:
                any_type_change = True
            _apply_title_patch_fields(title, fields)
            title.title_seq = incoming_seq
            results.append({
                "title_id": patch.title_id,
                "success": True,
                "title_obj": title,
            })
            any_success = True
        except Exception as exc:
            log.warning(f"Failed to patch title {patch.title_id}: {exc}")
            results.append({
                "title_id": patch.title_id,
                "success": False,
                "error": "Patch failed",
                "error_code": "patch_failed",
            })

    if any_success:
        # Area 2: sweep only when the batch shaped groups (a type write).
        # See patch_disc_title for the rationale; text-only batches are
        # plain row writes with no disc-wide side effects.
        if any_type_change:
            from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

            # See patch_disc_title: per-patch sync must not consensus-fill NULL primary types.
            # See the single-patch endpoint: report what the sync touches so the
            # client's per-title seq cache stays truthful (#775).
            sync_duplicate_group_labels_for_disc(
                db, disc_id, fill_null_type_from_consensus=False,
                collect_modified=synced_rows,
            )
        new_version = max(_get_titles_version(disc), current_version + 1)
        _set_titles_version(disc, new_version)
        if any_correction:
            _stamp_user_edit(disc)
        db.commit()
        current_version = new_version

    response_results: List[TitlePatchResult] = []
    for item in results:
        if item.get("success"):
            title_obj = item.get("title_obj")
            response_results.append(TitlePatchResult(
                title_id=item["title_id"],
                success=True,
                updated_title=_serialize_disc_title(title_obj) if title_obj else None,
            ))
        else:
            response_results.append(TitlePatchResult(
                title_id=item["title_id"],
                success=False,
                error=item.get("error"),
                error_code=item.get("error_code"),
                current_title=item.get("current_title"),
            ))

    patched_ids = {str(item.get("title_obj").id) for item in results
                   if item.get("success") and item.get("title_obj") is not None}
    synced_payloads = [
        _serialize_disc_title(t) for t in synced_rows
        if str(t.id) not in patched_ids
    ]
    updated_payloads = [
        r.updated_title for r in response_results
        if r.success and r.updated_title is not None
    ]
    # Area 4: same delta fan-out as the single-title endpoint.
    _emit_titles_changed_threadsafe(disc_id, [*updated_payloads, *synced_payloads], current_version)
    return TitlePatchBatchResponse(
        titles_version=current_version,
        results=response_results,
        synced_titles=synced_payloads or None,
    )


@router.get(
    "/{disc_id}/remaining-playlist-size",
    response_model=RemainingPlaylistSizeResponse,
)
def get_remaining_playlist_size(
    disc_id: str,
    db: Session = Depends(database.get_db),
):
    """Disk-pressure snapshot for the Path B iteration loop.

    Sums the size of titles that would still get ripped — non-ignored
    AND non-subsumed (m2ts wrapped by an mpls would double-count, so
    skip subsumed rows). Compares against a ceiling of
    min(200 GB, free_disk * 0.9) to decide whether the frontend can
    show the "Rip the rest" CTA.
    """
    import shutil
    from core.segment_reorder import rip_the_rest_threshold_bytes
    from core.utils import get_mkvauto_data

    disc = (
        db.query(db_models.Disc)
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")

    titles = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .all()
    )
    total_count = len(titles)
    ignored_count = sum(1 for t in titles if t.type == "ignore")
    total_size_b = sum(int(t.size or 0) for t in titles)
    remaining_size_b = sum(
        int(t.size or 0)
        for t in titles
        if t.type != "ignore" and getattr(t, "subsumed_by_title_id", None) is None
    )

    free_disk_b: int | None = None
    try:
        free_disk_b = shutil.disk_usage(str(get_mkvauto_data())).free
    except Exception:
        # Disk probe failure → threshold falls to 0 → CTA hidden. Frontend
        # treats this as "can't fit, ask the user to clear space" rather
        # than silently allowing a possibly-too-large rip.
        pass

    threshold_b = rip_the_rest_threshold_bytes(free_disk_b)
    has_remaining = remaining_size_b > 0 and (total_count - ignored_count) > 0
    allows_rip_rest = has_remaining and remaining_size_b <= threshold_b

    return RemainingPlaylistSizeResponse(
        disc_id=disc_id,
        remaining_size_b=remaining_size_b,
        total_size_b=total_size_b,
        ignored_count=ignored_count,
        total_count=total_count,
        free_disk_b=free_disk_b,
        threshold_b=threshold_b,
        allows_rip_rest=allows_rip_rest,
    )


@router.get("/{disc_id}/segment-flags", response_model=SegmentFlagPatchResponse)
def get_segment_flags(
    disc_id: str,
    db: Session = Depends(database.get_db),
):
    """Read the per-disc clip-obfuscation-flag dictionary.

    Frontend calls this on segment-reorder page load to hydrate the
    three-state per-tile flag UI. Same response shape as the PATCH
    endpoint so the page can reuse the same parsing path.
    """
    disc = (
        db.query(db_models.Disc)
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    return SegmentFlagPatchResponse(
        disc_id=disc_id,
        flags=dict(disc.segment_obfuscation_flags or {}),
    )


@router.patch("/{disc_id}/segment-flags", response_model=SegmentFlagPatchResponse)
def patch_segment_flag(
    disc_id: str,
    patch: SegmentFlagPatchRequest = Body(...),
    db: Session = Depends(database.get_db),
):
    """Set or clear one clip's obfuscation flag on a disc.

    Used by the Path B iteration loop: the user can mark individual clips
    they suspect or have confirmed are decoy/noise segments. The matcher
    consults these flags when scoring subsequence-superset candidates —
    `definitely` excludes mpls containing the clip, `potentially`
    rank-deprioritises.

    Per-disc scope (not per-job): the flag describes the physical disc and
    persists across job restarts AND across multiple rip attempts.
    """
    disc = (
        db.query(db_models.Disc)
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")

    current = dict(disc.segment_obfuscation_flags or {})
    if patch.flag is None:
        current.pop(patch.clip_id, None)
    else:
        current[patch.clip_id] = patch.flag
    # Reassign so SQLAlchemy's JSON-mutation tracker picks up the change.
    disc.segment_obfuscation_flags = current
    db.commit()
    return SegmentFlagPatchResponse(disc_id=disc_id, flags=current)


@router.get("/unified", response_model=DiscJobState)
async def get_unified_disc(
    disc_id: str | None = Query(None, description="Disc ID (UUID)"),
    disc_num: str | None = Query(None, description="Disc number"),
    mount_point: str | None = Query(None, description="Mount point"),
    include: str | None = Query(None, description="Comma-separated list: workflow,labels,release,job"),
    db: Session = Depends(database.get_db),
):
    """
    Unified endpoint for disc information.
    Supports querying by disc_id, disc_num+mount_point, or just mount_point.
    Use 'include' parameter to control data depth:
    - workflow: Include workflow context (labelForm, titles, etc.)
    - labels: Include label draft and payload
    - release: Include full release details
    - job: Include active job status
    
    Example: /discs/unified?disc_id=xxx&include=workflow,release,job
    """
    include_set = set((include or "").split(",")) if include else set()
    include_workflow = "workflow" in include_set
    include_labels = "labels" in include_set
    include_release = "release" in include_set
    include_job = "job" in include_set or True  # Always include job by default
    
    # If disc_id is provided, use it directly
    if disc_id:
        disc_record = (
            db.query(db_models.Disc)
            .options(
                joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
                joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
                joinedload(db_models.Disc.titles),
            )
            .filter(db_models.Disc.id == disc_id)
            .first()
        )
        
        if not disc_record:
            raise HTTPException(404, detail=f"Disc with id {disc_id} not found")
        
        mount_point = disc_record.mount_point
        if not mount_point:
            raise HTTPException(400, detail="Disc record has no mount_point")
        
        # Get disc_num from cache (no scan)
        cached_discs = await loop.run_in_executor(None, get_cached_discs)
        disc_num = None
        for disc_info in cached_discs:
            if disc_info.get("mount_point") == mount_point:
                disc_num = disc_info.get("disc_num")
                break
        
        if not disc_num:
            raise HTTPException(404, detail=f"Drive with mount_point {mount_point} not found in cache")
    elif disc_num and mount_point:
        # Use disc_num and mount_point
        pass
    elif mount_point:
        # Try to find disc_num from mount_point using cache (no scan)
        cached_discs = await loop.run_in_executor(None, get_cached_discs)
        disc_num = None
        for disc_info in cached_discs:
            if disc_info.get("mount_point") == mount_point:
                disc_num = disc_info.get("disc_num")
                break
        if not disc_num:
            raise HTTPException(404, detail=f"Drive with mount_point {mount_point} not found in cache")
    else:
        # No identifiers provided - try to get active job
        job = crud.get_most_recent_running_job(db)
        if job:
            payload = job.disc_payload or {}
            payload.update({"disc_num": job.disc_num, "mount_point": job.mount_point})
            disc_detail = _safe_disc_detail(payload, str(job.disc_num), str(job.mount_point))
            job_status = _job_to_status(job) if include_job else None
            return DiscJobState(disc=disc_detail, job=job_status)
        raise HTTPException(404, detail="No active job or drive provided")
    
    # Get disc info from cache (no scan)
    # Architecture: Drive Manager handles scanning, Disc Manager caches, API reads from cache
    loop = asyncio.get_running_loop()
    disc_info = await loop.run_in_executor(
        None,
        lambda: _get_disc_info_from_cache_or_scan(str(disc_num), mount_point, allow_scan=False)
    )
    
    if not disc_info:
        raise HTTPException(404, detail=f"Disc {disc_num} not found in cache")
    
    payload = disc_info.copy()
    
    # Get or create disc record
    disc_hash = payload.get("disc_hash")
    disc_record = None
    if disc_hash:
        try:
            disc_record = crud.persist_disc_scan_with_discdb(db, disc_hash, payload)
            crud.merge_pending_release_into_disc_info_dict(disc_record, payload)
            
            # Store disc scan info
            disc_scan_info = crud._extract_disc_scan_info(payload)
            if disc_scan_info:
                crud._store_disc_scan_info(db, disc_record, disc_scan_info)
            
            # Enrich payload with database data
            payload.setdefault("disc_id", str(disc_record.id))
            payload.setdefault("disc_number", disc_record.disc_number)
            payload.setdefault("discdb_disc_num", getattr(disc_record, "discdb_disc_num", None))
            payload.setdefault("disc_slug", disc_record.disc_slug)
            payload.setdefault("disc_name", disc_record.disc_name)
            payload.setdefault("disc_format", disc_record.format or payload.get("disc_format"))
            
            if disc_record.release and include_release:
                release = disc_record.release
                payload.setdefault("release_id", str(release.id))
                payload.setdefault("disc_group", release.slug)
                payload.setdefault("release_slug", release.slug)
                payload.setdefault("release_name", release.name)
                payload.setdefault("release_resolution", release.resolution)
                payload.setdefault("release_year", release.release_year)
                payload.setdefault("production_year", release.production_year)
                
                if release.movie:
                    movie = release.movie
                    payload.setdefault("movie_id", str(movie.id))
                    payload.setdefault("movie_name", movie.name or "")
                    payload.setdefault("movie_production_year", movie.production_year)
                    payload.setdefault("movie_tmdb_id", movie.tmdb_id)
                    payload.setdefault("movie_tmdb_type", movie.tmdb_type)
                    payload.setdefault("movie_cover_url", movie.cover_url)
                    payload.setdefault("movie_cover_path", movie.cover_path)
            elif include_release and payload.get("pending_release_id"):
                pr = (
                    db.query(db_models.Release)
                    .options(joinedload(db_models.Release.movie))
                    .filter(db_models.Release.id == str(payload["pending_release_id"]))
                    .first()
                )
                if pr:
                    payload.setdefault("disc_group", pr.slug)
                    payload.setdefault("release_slug", pr.slug)
                    payload.setdefault("release_name", pr.name)
                    payload.setdefault("release_resolution", pr.resolution)
                    payload.setdefault("release_year", pr.release_year)
                    if pr.movie:
                        movie = pr.movie
                        payload.setdefault("movie_id", str(movie.id))
                        payload.setdefault("movie_name", movie.name or "")
                        payload.setdefault("movie_production_year", movie.production_year)
                        payload.setdefault("movie_tmdb_id", movie.tmdb_id)
                        payload.setdefault("movie_tmdb_type", movie.tmdb_type)
                        payload.setdefault("movie_cover_url", movie.cover_url)
                        payload.setdefault("movie_cover_path", movie.cover_path)
        except Exception as exc:
            log.warning(f"Failed to ensure disc record for disc {disc_num}: {exc}")
    
    # Include labels if requested
    if include_labels and disc_record:
        label_draft = disc_record.label_draft if isinstance(disc_record.label_draft, dict) else {}
        if label_draft:
            payload["label_draft"] = label_draft
    
    # Get active job if requested
    job = None
    if include_job:
        job = crud.get_active_job_for_disc(db, str(disc_num), disc_hash)
    
    # Ensure movie_name is set
    if "movie_name" not in payload:
        payload["movie_name"] = payload.get("movie_name") or payload.get("show_title") or ""
    
    # Create disc detail
    disc_detail = _safe_disc_detail(payload, str(disc_num), str(mount_point))
    
    # Include workflow context if requested
    if include_workflow and disc_record:
        # Build workflow context similar to get_disc_workflow_context_by_id
        labelForm = _build_labelform_from_disc(disc_record, payload, active_job=job, db=db)
        # Note: Full workflow context would require more data, but this provides the essentials
        payload["workflow_context"] = {
            "labelForm": labelForm,
            "labelDraftProcessed": bool(disc_record.label_draft),
            "discNameLocked": bool(disc_record.disc_name),
            "discSlugLocked": bool(disc_record.disc_slug),
        }
    
    job_status = _job_to_status(job) if job else None
    return DiscJobState(disc=disc_detail, job=job_status)


@router.get("/current", response_model=DiscJobState)
async def current(
    disc_num: str | None = None,
    mount_point: str | None = None,
    db: Session = Depends(database.get_db),
):
    """
    Return the current disc info and any active job for that disc.
    Disc scanning is intentionally avoided unless a drive is explicitly provided.
    DEPRECATED: Use /discs/unified instead.
    """
    # If caller didn't provide a drive, only surface any active job; otherwise 404.
    if not disc_num or not mount_point:
        job = crud.get_most_recent_running_job(db)
        if job:
            payload = job.disc_payload or {}
            payload.update({"disc_num": job.disc_num, "mount_point": job.mount_point})
            disc_detail = _safe_disc_detail(payload, str(job.disc_num), str(job.mount_point))
            job_status = _job_to_status(job)
            return DiscJobState(disc=disc_detail, job=job_status)
        raise HTTPException(404, detail="No active job or drive provided")

    # Get disc info from cache (no scan)
    # Architecture: Drive Manager handles scanning, Disc Manager caches, API reads from cache
    loop = asyncio.get_running_loop()
    disc_info = await loop.run_in_executor(
        None,
        lambda: _get_disc_info_from_cache_or_scan(str(disc_num), mount_point, allow_scan=False)
    )
    
    if not disc_info:
        raise HTTPException(404, detail=f"Disc {disc_num} not found in cache")
    
    payload = disc_info.copy()

    # prefer job payload if there is an active job
    job = crud.get_active_job_for_disc(db, str(disc_num), payload.get("disc_hash"))
    if job and job.disc_payload:
        p = job.disc_payload or {}
        p.update({"disc_num": job.disc_num, "mount_point": job.mount_point})
        payload = p
    else:
        # No active job: best-effort create/link release+disc records from the scan payload.
        try:
            disc_rec = crud.ensure_disc_record_from_scan(db, str(disc_num), mount_point, payload)
            if disc_rec:
                payload.setdefault("disc_id", disc_rec.id)
                payload.setdefault("disc_number", disc_rec.disc_number)
                payload.setdefault("discdb_disc_num", getattr(disc_rec, "discdb_disc_num", None))
                payload.setdefault("disc_slug", disc_rec.disc_slug)
                payload.setdefault("disc_name", disc_rec.disc_name)
                payload.setdefault("disc_format", disc_rec.format or payload.get("disc_format"))
                if disc_rec.release:
                    release = disc_rec.release
                    payload.setdefault("release_id", release.id)
                    payload.setdefault("disc_group", release.slug)
                    payload.setdefault("release_slug", release.slug)
                    payload.setdefault("release_name", release.name)
                    # Include release metadata for Now Reading card
                    payload.setdefault("release_resolution", release.resolution)
                    payload.setdefault("release_year", release.release_year)
                    payload.setdefault("production_year", release.production_year)
                    # Include movie metadata if available (hierarchy: movie.name -> release.name -> title.name)
                    if release.movie:
                        movie = release.movie
                        payload.setdefault("movie_id", movie.id)
                        payload.setdefault("movie_name", movie.name or "")
                        payload.setdefault("movie_production_year", movie.production_year)
                        payload.setdefault("movie_tmdb_id", movie.tmdb_id)
                        payload.setdefault("movie_tmdb_type", movie.tmdb_type)
                        payload.setdefault("movie_cover_url", movie.cover_url)
                        payload.setdefault("movie_cover_path", movie.cover_path)
        except Exception as exc:
            log.warning("Failed to ensure disc record for disc %s: %s", disc_num, exc)
    
    # Ensure movie_name is set (with backward compat for show_title)
    if "movie_name" not in payload:
        payload["movie_name"] = payload.get("movie_name") or payload.get("show_title") or ""

    # safe DiscDetail creation
    disc_detail = _safe_disc_detail(payload, str(disc_num), str(mount_point))

    job_status = _job_to_status(job) if job else None
    return DiscJobState(disc=disc_detail, job=job_status)


@router.post("/cache")
async def update_cache(payload: dict):
    """
    Internal helper for drive-manager to seed backend cache so SSE can emit fresh payloads
    without waiting for a client refresh.
    """
    disc_num = payload.get("disc_num")
    mount_point = payload.get("mount_point")
    if not disc_num or not mount_point:
        raise HTTPException(status_code=400, detail="disc_num and mount_point required")
    enriched = _enrich_with_disc_record(
        hydrate_disc_payload(str(disc_num), str(mount_point), payload),
        str(disc_num),
        str(mount_point),
    )
    set_cached(str(disc_num), enriched)
    return {"status": "ok"}


def _enrich_with_disc_record(payload: dict, disc_num: str, mount_point: str) -> dict:
    """
    Ensure a disc record exists for this payload and attach identifiers/metadata
    so the frontend receives disc_id/disc_number/disc_slug on first load.
    """
    db = None
    try:
        db = database.SessionLocal()
        disc_rec = crud.ensure_disc_record_from_scan(db, disc_num, mount_point, payload)
        if disc_rec:
            crud.merge_pending_release_into_disc_info_dict(disc_rec, payload)
            payload.setdefault("disc_id", disc_rec.id)
            payload.setdefault("disc_number", disc_rec.disc_number)
            payload.setdefault("discdb_disc_num", getattr(disc_rec, "discdb_disc_num", None))
            payload.setdefault("disc_slug", disc_rec.disc_slug)
            payload.setdefault("disc_name", disc_rec.disc_name)
            payload.setdefault("disc_format", disc_rec.format or payload.get("disc_format"))
            if disc_rec.release:
                release = disc_rec.release
                payload.setdefault("release_id", release.id)
                payload.setdefault("disc_group", release.slug)
                payload.setdefault("release_slug", release.slug)
                payload.setdefault("release_name", release.name)
                # Include release metadata for Now Reading card
                payload.setdefault("release_resolution", release.resolution)
                payload.setdefault("release_year", release.release_year)
                payload.setdefault("production_year", release.production_year)
                # Include movie metadata if available
                if release.movie:
                    movie = release.movie
                    payload.setdefault("movie_id", movie.id)
                    payload.setdefault("movie_name", movie.name)
                    payload.setdefault("movie_production_year", movie.production_year)
                    payload.setdefault("movie_tmdb_id", movie.tmdb_id)
                    payload.setdefault("movie_tmdb_type", movie.tmdb_type)
                    payload.setdefault("movie_cover_url", movie.cover_url)
                    payload.setdefault("movie_cover_path", movie.cover_path)
            elif payload.get("pending_release_id"):
                pr = (
                    db.query(db_models.Release)
                    .options(joinedload(db_models.Release.movie))
                    .filter(db_models.Release.id == str(payload["pending_release_id"]))
                    .first()
                )
                if pr:
                    payload.setdefault("disc_group", pr.slug)
                    payload.setdefault("release_slug", pr.slug)
                    payload.setdefault("release_name", pr.name)
                    if pr.movie:
                        m = pr.movie
                        payload.setdefault("movie_id", m.id)
                        payload.setdefault("movie_name", m.name)
                        payload.setdefault("movie_production_year", m.production_year)
                        payload.setdefault("movie_tmdb_id", m.tmdb_id)
                        payload.setdefault("movie_tmdb_type", m.tmdb_type)
                        payload.setdefault("movie_cover_url", m.cover_url)
                        payload.setdefault("movie_cover_path", m.cover_path)
    except Exception as exc:
        log.warning("Failed to enrich disc payload with DB record: %s", exc)
    finally:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
    return payload


@router.post("/{disc_id}/boxsets", response_model=Dict[str, Any])
def create_boxset_for_disc(
    disc_id: str,
    boxset_data: BoxsetCreate = Body(...),
    movie_id: str = Query(..., description="Movie ID for release creation"),
    db: Session = Depends(database.get_db)
):
    """
    Create boxset, create release for movie (linked to boxset), and link release to disc.
    Returns boxset and release summaries, and emits workflow context update via WebSocket.
    """
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    _log_rb(
        "POST /discs/{disc_id}/boxsets start",
        disc_id=disc_id,
        movie_id=movie_id,
        boxset_name=(boxset_data.name or "")[:80],
        year=boxset_data.year,
        prior_release_id=getattr(disc, "release_id", None),
        content_hash_prefix=(disc.content_hash or "")[:16] if disc else None,
    )
    if not disc:
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="disc_not_found")
        raise HTTPException(404, detail="Disc not found")

    # Verify movie exists
    movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
    if not movie:
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="movie_not_found", movie_id=movie_id)
        raise HTTPException(404, detail="Movie not found")
    
    # Validate required fields and formats
    from api.routers.releases import _validate_release_year, _validate_upc, _validate_cover_url
    
    if not boxset_data.name or not boxset_data.name.strip():
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="validation", field="name")
        raise HTTPException(400, detail="Boxset name is required")
    
    if not _validate_release_year(boxset_data.year):
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="validation", field="year")
        raise HTTPException(400, detail="Boxset year must be a 4-digit number (1000-9999)")
    
    if not _validate_upc(boxset_data.upc):
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="validation", field="upc")
        raise HTTPException(400, detail="UPC must be exactly 12 numeric digits")
    
    if not _validate_cover_url(boxset_data.cover_front_url):
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="validation", field="cover_front_url")
        raise HTTPException(400, detail="Front cover URL must be a valid http:// or https:// URL")
    
    # Create boxset
    from core.utils import slugify
    slug = slugify(boxset_data.name)
    if not slug:
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="validation", field="slug")
        raise HTTPException(400, detail="Boxset name is required")
    
    # Check if slug already exists
    existing_boxset = crud.get_boxset_by_slug(db, slug)
    if existing_boxset:
        _log_rb(
            "POST /discs/.../boxsets failed",
            disc_id=disc_id,
            reason="boxset_slug_conflict",
            slug=slug,
            existing_boxset_id=existing_boxset.id,
        )
        raise HTTPException(409, detail=f"Boxset with slug '{slug}' already exists")
    
    boxset = crud.get_or_create_boxset(db, {
        "slug": slug,
        "name": boxset_data.name,
        "title": boxset_data.title or boxset_data.name,
        "sort_title": boxset_data.sort_title,
        "year": boxset_data.year,
        "upc": boxset_data.upc,
        "asin": boxset_data.asin,
        "locale": boxset_data.locale,
        "region_code": boxset_data.region_code,
        "cover_front_url": boxset_data.cover_front_url,
        "cover_back_url": boxset_data.cover_back_url,
        "release_date": boxset_data.release_date,
    })
    
    if not boxset:
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="get_or_create_boxset_empty")
        raise HTTPException(400, detail="Failed to create boxset")

    _log_rb(
        "boxset row ready",
        disc_id=disc_id,
        boxset_id=boxset.id,
        boxset_slug=boxset.slug,
    )
    
    # Create release for movie, linked to boxset
    # Use boxset slug as release slug
    release_payload = {
        "movie_id": movie_id,
        "boxset_id": boxset.id,
        "release_name": boxset_data.name,
        "release_year": boxset_data.year,
        "upc": boxset_data.upc,
        "asin": boxset_data.asin,
        "cover_front_url": boxset_data.cover_front_url,
        "cover_back_url": boxset_data.cover_back_url,
    }
    
    _log_rb(
        "calling get_or_create_release for boxset flow",
        disc_id=disc_id,
        boxset_id=boxset.id,
        movie_id=movie_id,
        payload_keys=sorted(release_payload.keys()),
    )
    target_release = crud.get_or_create_release(db, release_payload, disc.content_hash)
    if not target_release:
        _log_rb("POST /discs/.../boxsets failed", disc_id=disc_id, reason="get_or_create_release_empty")
        raise HTTPException(400, detail="Failed to create release")

    _log_rb(
        "release resolved",
        disc_id=disc_id,
        release_id=target_release.id,
        release_slug=target_release.slug,
        release_boxset_id=target_release.boxset_id,
        target_boxset_id=boxset.id,
    )
    
    # Ensure release is linked to boxset and update slug/metadata
    # Use add_release_to_boxset to properly update slug from "pending" to boxset slug
    if target_release.boxset_id != boxset.id:
        from api.crud import add_release_to_boxset
        _log_rb(
            "add_release_to_boxset",
            disc_id=disc_id,
            release_id=target_release.id,
            from_boxset_id=target_release.boxset_id,
            to_boxset_id=boxset.id,
        )
        target_release = add_release_to_boxset(db, boxset, target_release)
    
    # Link release to disc
    disc.release_id = target_release.id

    # Sync label_draft with release assignment
    from api.crud import sync_disc_label_draft_with_release
    sync_disc_label_draft_with_release(disc, target_release)

    # Normalize disc numbers for consistent created_at ordering
    db.flush()
    from api.crud import normalize_disc_numbers_for_release
    disc_number_map = normalize_disc_numbers_for_release(db, target_release)
    disc.disc_number = disc_number_map.get(disc.id, disc.disc_number)

    _log_rb(
        "disc linked + disc_number",
        disc_id=disc_id,
        release_id=target_release.id,
        disc_number=disc.disc_number,
        label_draft_keys=sorted((disc.label_draft or {}).keys()) if isinstance(disc.label_draft, dict) else None,
    )
    
    db.commit()
    # Invalidate cache — same reason as create_release_for_disc below.
    invalidate_workflow_context_cache(disc_id=disc_id)
    db.refresh(boxset)
    db.refresh(target_release)
    db.refresh(disc)

    # Fetch updated workflow context
    context = get_disc_workflow_context_by_id(disc_id, db)
    
    # Emit workflow context change notification via WebSocket
    try:
        from api.routers.websockets import _emit_to_disc_workflow
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']))
        except RuntimeError:
            # No running loop - try to get app reference
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']), loop)
            except Exception as exc:
                log.warning(f"Failed to schedule websocket emission for disc {disc_id}: {exc}")
    except Exception as exc:
        log.warning(f"Failed to emit workflow context change notification to websocket for disc {disc_id}: {exc}")
    
    # Build summaries
    from api.routers.releases import _release_summary
    release_summary = _release_summary(target_release, db)
    
    release_count = db.query(db_models.Release).filter(
        db_models.Release.boxset_id == boxset.id
    ).count()
    
    boxset_summary = BoxsetSummary(
        id=boxset.id,
        slug=boxset.slug,
        name=boxset.name,
        title=boxset.title,
        sort_title=boxset.sort_title,
        upc=boxset.upc,
        asin=boxset.asin,
        year=boxset.year,
        locale=boxset.locale,
        region_code=boxset.region_code,
        cover_front_url=boxset.cover_front_url,
        cover_back_url=boxset.cover_back_url,
        image_url=boxset.image_url,
        release_date=boxset.release_date,
        finalized=boxset.finalized,
        finalized_at=boxset.finalized_at,
        release_count=release_count,
    )
    
    invalidate_options_cache()
    _log_rb(
        "POST /discs/.../boxsets done",
        disc_id=disc_id,
        boxset_id=boxset.id,
        release_id=target_release.id,
        linked=True,
    )
    return {
        "boxset": boxset_summary,
        "release": release_summary,
        "linked": True
    }


@router.post("/{disc_id}/movies", response_model=Dict[str, Any])
def create_movie_for_disc(
    disc_id: str,
    movie_data: MovieCreate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Ensure-and-link semantics: if a Movie with the supplied ``tmdb_id``
    already exists, reuse it; otherwise create a new one. Either way,
    store ``movie_id`` in the disc's ``label_draft`` and return the movie
    summary.

    Upsert (rather than 400-on-duplicate) is the right model here because
    the client's intent is "link this TMDB title to this disc" — whether
    the row was already in our DB from a previous label or backfill is an
    implementation detail. Both the URL-paste and TMDB-suggestion frontend
    flows depend on this being idempotent (#389 follow-up: clicking "Use
    this" twice, or after a TMDB-backfill seeded the movie, must not
    surface "already exists" errors that block progression).

    Does NOT create a release — release creation is postponed until user
    selects/creates a release. Emits workflow context update via WebSocket.
    """
    # Get disc
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        raise HTTPException(404, detail="Disc not found")

    # Reuse an existing movie when the tmdb_id matches; otherwise create.
    movie = None
    if movie_data.tmdb_id:
        movie = (
            db.query(db_models.Movie)
            .filter(db_models.Movie.tmdb_id == movie_data.tmdb_id)
            .first()
        )
    if movie is None:
        movie = db_models.Movie(
            name=movie_data.name,
            production_year=movie_data.production_year,
            tmdb_id=movie_data.tmdb_id,
            tmdb_type=movie_data.tmdb_type,
            cover_url=movie_data.cover_url,
        )
        db.add(movie)
        db.commit()
        db.refresh(movie)
    
    # Store movie_id and group_type in disc's label_draft (no release yet)
    label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    label_draft["movie_id"] = movie.id
    label_draft["group_type"] = "series" if (movie_data.tmdb_type or "").lower() == "tv" else "movie"
    disc.label_draft = label_draft

    db.commit()
    # Invalidate cache — same reason as create_release_for_disc.
    invalidate_workflow_context_cache(disc_id=disc_id)
    db.refresh(movie)
    db.refresh(disc)

    # Fetch updated workflow context
    context = get_disc_workflow_context_by_id(disc_id, db)
    
    # Emit workflow context update via WebSocket
    try:
        from api.routers.websockets import _emit_to_disc_workflow
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']))
        except RuntimeError:
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']), loop)
            except Exception as exc:
                log.warning(f"Failed to schedule websocket emission for disc {disc_id}: {exc}")
    except Exception as exc:
        log.warning(f"Failed to emit workflow context update to websocket for disc {disc_id}: {exc}")
    
    # Build summary
    from api.routers.movies import _movie_summary
    movie_summary = _movie_summary(movie)
    
    invalidate_options_cache()
    return {
        "movie": movie_summary,
    }


@router.post("/{disc_id}/releases", response_model=Dict[str, Any])
def create_release_for_disc(
    disc_id: str,
    release_data: Dict[str, Any] = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Create release and link to disc.
    If disc has movie_id in label_draft, include it in release creation.
    Returns release summary, and emits workflow context update via WebSocket.
    """
    # Get disc
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if not disc:
        _log_rb("POST /discs/.../releases failed", disc_id=disc_id, reason="disc_not_found")
        raise HTTPException(404, detail="Disc not found")

    body_keys = sorted(release_data.keys()) if isinstance(release_data, dict) else []
    _log_rb(
        "POST /discs/{disc_id}/releases start",
        disc_id=disc_id,
        prior_release_id=disc.release_id,
        request_keys=body_keys,
        content_hash_prefix=(disc.content_hash or "")[:16],
    )
    
    # Check if disc has movie_id in label_draft and include it in release_data
    label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    if label_draft.get("movie_id") and "movie_id" not in release_data:
        release_data["movie_id"] = label_draft["movie_id"]
    if "tmdb_id" not in release_data and label_draft.get("movie_id"):
        movie = db.query(db_models.Movie).filter(db_models.Movie.id == label_draft["movie_id"]).first()
        if movie and movie.tmdb_id:
            release_data["tmdb_id"] = movie.tmdb_id

    # When caller sends only movie_id (e.g. from film-step selection), build minimal release payload from movie
    # so the same create/link path works for fresh rips and "went back and changed movie".
    movie_id = release_data.get("movie_id")
    if movie_id and not release_data.get("boxset_id"):
        movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
        if movie:
            if not release_data.get("release_name") or not str(release_data.get("release_name", "")).strip():
                release_data["release_name"] = (movie.name or "").strip() or "Unknown"
            if release_data.get("release_year") is None or not (1000 <= int(release_data.get("release_year") or 0) <= 9999):
                py = getattr(movie, "production_year", None)
                release_data["release_year"] = py if (py is not None and 1000 <= py <= 9999) else 2000
            if not release_data.get("upc") or not (str(release_data.get("upc", "")).strip() and len(str(release_data.get("upc", "")).strip()) in (8, 12, 13, 14)):
                release_data["upc"] = release_data.get("upc") or "0000000000000"
            cover = release_data.get("cover_front_url") or getattr(movie, "cover_url", None) or ""
            if not (cover and (str(cover).startswith("http://") or str(cover).startswith("https://"))):
                release_data["cover_front_url"] = "https://via.placeholder.com/300"
            # Set group_type from label_draft or movie so new releases get correct type (series vs movie)
            if not release_data.get("group_type") and not release_data.get("type"):
                release_data["group_type"] = (
                    label_draft.get("group_type")
                    or ("series" if getattr(movie, "tmdb_type", None) == "tv" else "movie")
                )

    # #711: a boxset-member release must still get a name. The block above only
    # defaults for standalone releases (not boxset_id), relying on the boxset
    # name to flow in via _merge_boxset_into_release_payload during
    # get_or_create_release. When that doesn't take effect the release is created
    # nameless and the label workflow silently stalls (no finalize, no error).
    # Set the name explicitly here — boxset name/title, else the movie name — so
    # it is in the payload regardless of the internal merge.
    if movie_id and release_data.get("boxset_id"):
        rn = release_data.get("release_name")
        if not rn or not str(rn).strip():
            derived = None
            bx = db.query(db_models.Boxset).filter(db_models.Boxset.id == release_data["boxset_id"]).first()
            if bx:
                derived = (bx.name or bx.title or "").strip() or None
            if not derived:
                mv = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
                derived = (mv.name or "").strip() if (mv and mv.name) else None
            release_data["release_name"] = derived or "Unknown"

    # If no movie_id block ran but we still need group_type, set from label_draft or default
    if not release_data.get("group_type") and not release_data.get("type") and label_draft.get("group_type"):
        release_data["group_type"] = label_draft["group_type"]

    # Get old release before creating/linking new one
    old_release_id = disc.release_id

    _log_rb(
        "release payload after label_draft merge + defaults",
        disc_id=disc_id,
        movie_id=release_data.get("movie_id"),
        boxset_id=release_data.get("boxset_id"),
        group_type=release_data.get("group_type") or release_data.get("type"),
        release_name_preview=str(release_data.get("release_name") or "")[:60] or None,
        old_release_id=old_release_id,
    )

    # Create or find release
    target_release = crud.get_or_create_release(db, release_data, disc.content_hash)
    if not target_release:
        _log_rb("POST /discs/.../releases failed", disc_id=disc_id, reason="get_or_create_release_empty")
        raise HTTPException(400, detail="Failed to create release")

    _log_rb(
        "release resolved",
        disc_id=disc_id,
        release_id=target_release.id,
        release_slug=target_release.slug,
        release_boxset_id=target_release.boxset_id,
        movie_id_on_release=str(target_release.movie_id) if target_release.movie_id else None,
    )
    
    # Link release to disc
    disc.release_id = target_release.id

    # Sync label_draft with release assignment
    from api.crud import sync_disc_label_draft_with_release
    sync_disc_label_draft_with_release(disc, target_release)

    # Cleanup orphaned release if disc was previously linked to a different release
    if old_release_id and old_release_id != target_release.id:
        old_release = db.query(db_models.Release).filter(db_models.Release.id == old_release_id).first()
        if old_release:
            _log_rb(
                "cleanup previous release after reassign",
                disc_id=disc_id,
                old_release_id=old_release_id,
                new_release_id=target_release.id,
            )
            from api.crud import cleanup_orphaned_release
            cleanup_orphaned_release(db, old_release)
    
    # Normalize disc numbers for consistent created_at ordering
    db.flush()
    from api.crud import normalize_disc_numbers_for_release
    disc_number_map = normalize_disc_numbers_for_release(db, target_release)
    disc.disc_number = disc_number_map.get(disc.id, disc.disc_number)

    _log_rb(
        "disc linked + disc_number",
        disc_id=disc_id,
        release_id=target_release.id,
        disc_number=disc.disc_number,
        label_draft_keys=sorted((disc.label_draft or {}).keys()) if isinstance(disc.label_draft, dict) else None,
    )
    
    db.commit()
    # Invalidate the workflow-context response cache so the WS-triggered
    # debounced refetch (fired via _emit_to_disc_workflow below) doesn't hit
    # the stale pre-link snapshot cached at page-load. Without this, the
    # frontend's updateContext(fetched) full-replace clobbers the just-linked
    # release — user sees the link appear, then disappear ~300ms later when
    # the debounced fetch resolves. TTL is 10s (see _CONTEXT_CACHE_TTL_SECONDS
    # at :77), so the race window is any user action within 10s of the last
    # workflow-context read.
    invalidate_workflow_context_cache(disc_id=disc_id)
    db.refresh(target_release)
    db.refresh(disc)

    # Fetch updated workflow context
    context = get_disc_workflow_context_by_id(disc_id, db)

    # Emit workflow context change notification via WebSocket
    try:
        from api.routers.websockets import _emit_to_disc_workflow
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']))
        except RuntimeError:
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(_emit_to_disc_workflow(disc_id, changed_fields=['labelForm']), loop)
            except Exception as exc:
                log.warning(f"Failed to schedule websocket emission for disc {disc_id}: {exc}")
    except Exception as exc:
        log.warning(f"Failed to emit workflow context change notification to websocket for disc {disc_id}: {exc}")

    # Build summary
    from api.routers.releases import _release_summary
    release_summary = _release_summary(target_release, db)
    
    invalidate_options_cache()
    _log_rb(
        "POST /discs/.../releases done",
        disc_id=disc_id,
        release_id=target_release.id,
        linked=True,
    )
    return {
        "release": release_summary,
        "linked": True
    }


@router.post("/workflow-context/boxsets", response_model=Dict[str, Any])
def create_boxset_for_disc_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    movie_id: str = Query(..., description="Movie ID for release creation"),
    boxset_data: BoxsetCreate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Create boxset for disc by mount_point, create release for movie (linked to boxset), and link release to disc.
    Finds disc by mount_point first, then follows same pattern as disc_id endpoint.
    """
    # Find disc by mount_point
    disc = db.query(db_models.Disc).filter(db_models.Disc.mount_point == mount_point).order_by(db_models.Disc.created_at.desc()).first()
    if not disc:
        _log_rb(
            "POST /discs/workflow-context/boxsets failed",
            mount_point=mount_point,
            reason="disc_not_found",
        )
        raise HTTPException(404, detail=f"Disc with mount_point '{mount_point}' not found")

    _log_rb(
        "POST /discs/workflow-context/boxsets resolved mount → disc_id",
        mount_point=mount_point,
        disc_id=disc.id,
        movie_id=movie_id,
    )
    # Reuse the disc_id endpoint logic by calling it directly
    # We need to pass the parameters correctly
    return create_boxset_for_disc(str(disc.id), boxset_data, movie_id, db)


@router.post("/workflow-context/movies", response_model=Dict[str, Any])
def create_movie_for_disc_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    movie_data: MovieCreate = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Create movie for disc by mount_point, create/find release for movie, and link release to disc.
    Finds disc by mount_point first, then follows same pattern as disc_id endpoint.
    """
    # Find disc by mount_point
    disc = db.query(db_models.Disc).filter(db_models.Disc.mount_point == mount_point).order_by(db_models.Disc.created_at.desc()).first()
    if not disc:
        raise HTTPException(404, detail=f"Disc with mount_point '{mount_point}' not found")
    
    # Reuse the disc_id endpoint logic
    return create_movie_for_disc(str(disc.id), movie_data, db)


@router.post("/workflow-context/releases", response_model=Dict[str, Any])
def create_release_for_disc_by_mount(
    mount_point: str = Query(..., description="Mount point of the disc drive"),
    release_data: Dict[str, Any] = Body(...),
    db: Session = Depends(database.get_db)
):
    """
    Create release for disc by mount_point and link to disc.
    Finds disc by mount_point first, then follows same pattern as disc_id endpoint.
    """
    # Find disc by mount_point
    disc = db.query(db_models.Disc).filter(db_models.Disc.mount_point == mount_point).order_by(db_models.Disc.created_at.desc()).first()
    if not disc:
        _log_rb(
            "POST /discs/workflow-context/releases failed",
            mount_point=mount_point,
            reason="disc_not_found",
        )
        raise HTTPException(404, detail=f"Disc with mount_point '{mount_point}' not found")

    _log_rb(
        "POST /discs/workflow-context/releases resolved mount → disc_id",
        mount_point=mount_point,
        disc_id=disc.id,
    )
    # Reuse the disc_id endpoint logic
    return create_release_for_disc(str(disc.id), release_data, db)


# ────────────────────────────────────────────────────────────────
# Primary duplicate selection: set-primary endpoint
# ────────────────────────────────────────────────────────────────


@router.post("/{disc_id}/titles/{title_id}/set-primary")
def set_primary_title(
    disc_id: str,
    title_id: str,
    db: Session = Depends(database.get_db),
):
    """Swap the primary title within a duplicate group.

    Copies labeling metadata from the current primary to the target title,
    clears labeling metadata on the old primary, then enforces Option B:
    primary is active; every other row in the group is type=ignore with
    cleared labeling fields (postprocess skips ignore titles per core.disc).
    Per-title ``comment`` (file identity) is not part of the swapped tuple and
    stays on each physical variant row.
    """
    from core.duplicate_group_sync import (
        DUPLICATE_LABEL_METADATA_FIELDS,
        demote_duplicate_secondaries_in_group,
    )

    target = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id, db_models.DiscTitle.id == title_id)
        .first()
    )
    if not target:
        raise HTTPException(404, detail="Title not found")

    # Determine group by segment_map
    if not target.segment_map:
        raise HTTPException(400, detail="Title has no segment_map — cannot determine duplicate group")

    from core.duplicate_info import _normalize_segment_map
    norm_seg = _normalize_segment_map(target.segment_map)
    if not norm_seg:
        raise HTTPException(400, detail="Title has empty segment_map")

    # Find all titles in the same group
    all_titles = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .all()
    )
    group_titles = [
        t for t in all_titles
        if _normalize_segment_map(t.segment_map) == norm_seg
    ]

    if len(group_titles) <= 1:
        raise HTTPException(400, detail="Title is not part of a duplicate group")

    # Find current primary
    current_primary = next((t for t in group_titles if t.active is True), None)

    if current_primary and current_primary.id != title_id:
        # Swap metadata: copy from current primary to target, then clear old primary fields
        for field in DUPLICATE_LABEL_METADATA_FIELDS:
            value = getattr(current_primary, field, None)
            setattr(target, field, value)
            setattr(current_primary, field, None)

    demote_duplicate_secondaries_in_group(group_titles, primary_id=str(target.id))

    # Bump titles version on the disc (for frontend cache invalidation)
    disc = db.query(db_models.Disc).filter(db_models.Disc.id == disc_id).first()
    if disc:
        _set_titles_version(disc, _get_titles_version(disc) + 1)

    db.commit()

    # Re-query group for response (fresh state)
    db.refresh(target)
    group_titles = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .all()
    )
    group_titles = [
        t for t in group_titles
        if _normalize_segment_map(t.segment_map) == norm_seg
    ]
    # Same staleness trap as ungroup-duplicate: this rewrites active/type across
    # the group, so a cached workflow-context would serve the old primary.
    invalidate_workflow_context_cache(disc_id=disc_id)
    return {"titles": [_serialize_disc_title(t) for t in group_titles]}


@router.post("/{disc_id}/titles/{title_id}/ungroup-duplicate")
def ungroup_duplicate(
    disc_id: str,
    title_id: str,
    db: Session = Depends(database.get_db),
):
    """Toggle the per-title force_independent_group flag.

    Used when a title sits in a sorted-segment-set dedupe group that the
    user knows is a false positive — e.g. two real playlists that share
    segments but differ in audio/subs. Setting the flag makes
    `duplicate_info.attach_duplicate_info` skip the row at grouping
    time so it renders as its own left-rail entry.

    Idempotent in spirit but the endpoint actually TOGGLES so the same
    button label/action can re-group an already-split title. Frontend
    surfaces "Ungroup" vs "Re-group" based on the current flag value
    in the workflow-context payload.
    """
    target = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id, db_models.DiscTitle.id == title_id)
        .first()
    )
    if not target:
        raise HTTPException(404, detail="Title not found")
    target.force_independent_group = not bool(target.force_independent_group)
    db.commit()
    db.refresh(target)
    # This changes dedupe-group membership, and workflow-context responses are
    # cached for _CONTEXT_CACHE_TTL_SECONDS. Without invalidating, a client that
    # refetches straight after this call — which is exactly what the Ungroup
    # button does — gets the pre-ungroup grouping back for up to 10s and the
    # left rail appears not to have changed at all (mkv-auto-release#8).
    invalidate_workflow_context_cache(disc_id=disc_id)
    return {
        "title_id": title_id,
        "force_independent_group": bool(target.force_independent_group),
    }
