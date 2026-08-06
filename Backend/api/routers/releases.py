import logging
import os
import uuid
import time
from datetime import datetime
import shutil
import json
from pathlib import Path
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session, joinedload, selectinload, load_only
from sqlalchemy import or_

from api import database, crud
from api import models as db_models
from core.title_type_normalize import normalize_title_type_for_api as _normalize_title_type


def _invalidate_options() -> None:
    """Invalidate cached workflow options after release/boxset mutation."""
    try:
        from api.routers.discs import invalidate_options_cache
        invalidate_options_cache()
    except Exception:
        pass  # Best-effort; don't fail the request if cache invalidation fails


from api.schemas import (
    ReleaseSummary,
    DiscSummary,
    LabelRequest,
    DiscMetadataUpdate,
    ReleaseMetadataPatch,
    TitleStreamRecord,
    DiscRecord,
    ReleaseRecord,
    ReleaseFullResponse,
    BoxsetFullResponse,
    DiscWithJobStatus,
    DiscTitleRecord,
    TitleLabel,
    DiscMetadataPatch,
    PatchRequest,
    MovieSummary,
    MovieRecord,
    BoxsetSummary,
    BoxsetRecord,
    BoxsetCreate,
    BoxsetUpdate,
    LibraryResponse,
    LibraryReattachReport,
    LibraryReattachMatch,
    LibraryReattachConflict,
    RenameResponse,
    TitleSummary,
)
from fastapi.responses import JSONResponse
from core.utils import build_release_slug, get_mkvauto_root, slugify, is_dev_mode, get_export_root
from core.job_paths import JobPaths
from core.devmode import validate_against_repo, REPORT_NAME_TEMPLATE
from core.job_state import apply_job_state, apply_job_state_devmode, StateViolation, StageState, normalize_state_updates, validate_job_state_transition
from core.stage_backup import create_stage_backup, backup_files, restore_stage_backup, restore_files, get_stage_backup_dir

logger = logging.getLogger(__name__)


def _compute_release_slug(rel: db_models.Release | None, payload: dict | None = None) -> str | None:
    """
    Build a release slug from release name (if available) or release year + disc format.
    - If release name exists: release_name-2020 (lowercase, spaces replaced with _)
    - If no release name: 2020-uhd (release year + highest disc type)
    Format precedence: UHD > Blu-Ray > DVD.
    """
    payload = payload or {}
    year = payload.get("release_year") or (getattr(rel, "release_year", None) if rel else None)
    if not year:
        return None
    
    # Check if release name exists
    release_name = payload.get("release_name") or (getattr(rel, "name", None) if rel else None)
    if release_name and release_name.strip():
        # Use release name: lowercase, spaces replaced with _
        # First slugify to normalize, then replace dashes (from spaces) with underscores
        name_slug = slugify(release_name).replace("-", "_")
        return f"{name_slug}-{year}"
    
    # No release name: use year + format
    best_fmt: str | None = None
    # Inspect existing discs for the highest format we have stored.
    if rel and getattr(rel, "discs", None):
        for d in rel.discs:
            best_fmt = crud._best_format(best_fmt, getattr(d, "format", None))
    # Also consider incoming payload hints (e.g., from metadata update).
    best_fmt = crud._best_format(best_fmt, payload.get("disc_format") or payload.get("format"))
    fmt_slug = crud._format_slug(best_fmt)
    if fmt_slug:
        return f"{year}-{fmt_slug}"
    # Fallback: year-release
    return f"{year}-{slugify('release')}"


def _validate_release_year(year: int) -> bool:
    """Check if year is 4-digit (1000-9999)."""
    return isinstance(year, int) and 1000 <= year <= 9999


def _validate_upc(upc: str) -> bool:
    """Check if UPC is exactly 12 numeric digits."""
    if not upc or not isinstance(upc, str):
        return False
    return upc.isdigit() and len(upc) == 12


def _validate_cover_url(url: str) -> bool:
    """Check if URL is valid http:// or https:// URL."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith("http://") or url.startswith("https://")


def _validate_tmdb_url(url: str) -> bool:
    """Check if URL is HTTPS from themoviedb.org."""
    if not url or not isinstance(url, str):
        return False
    url = url.strip()
    return url.startswith("https://www.themoviedb.org/") or url.startswith("https://themoviedb.org/")
from core import discdb_finalize
from api.routers.jobs import _build_job_status, _derive_pipeline

log = logging.getLogger("api.routers.releases")


def _log_rb(msg: str, **kwargs: Any) -> None:
    """Structured info for release/boxset flows (grep: release/boxset)."""
    parts = " ".join(f"{k}={v!r}" for k, v in sorted(kwargs.items()) if v is not None)
    log.info("[release/boxset] %s%s", msg, (" " + parts) if parts else "")


router = APIRouter(prefix="/releases", tags=["releases"])


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _get_release_by_id(db: Session, rel_id: str):
    """
    Fetch release strictly by id. Slug lookups are intentionally not supported.
    """
    return (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .options(
            joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
            joinedload(db_models.Release.boxset),  # type: ignore
        )
        .filter(crud.models.Release.id == rel_id)  # type: ignore
        .first()
    )


def _get_release_by_id_or_slug(db: Session, id_or_slug: str, load_discs: bool = False):
    """
    Fetch release by id first, then by slug if not found.
    Used by list_release_discs so the frontend can pass either identifier.
    When load_discs is True, eager-load discs and their jobs so rel.discs is populated.
    """
    if load_discs:
        rel = (
            db.query(crud.models.Release)  # type: ignore[attr-defined]
            .options(
                joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
                joinedload(db_models.Release.boxset),  # type: ignore
                joinedload(db_models.Release.discs).joinedload(db_models.Disc.jobs),  # type: ignore
            )
            .filter(crud.models.Release.id == id_or_slug)  # type: ignore
            .first()
        )
        if rel is not None:
            return rel
        return (
            db.query(crud.models.Release)  # type: ignore[attr-defined]
            .options(
                joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
                joinedload(db_models.Release.boxset),  # type: ignore
                joinedload(db_models.Release.discs).joinedload(db_models.Disc.jobs),  # type: ignore
            )
            .filter(crud.models.Release.slug == id_or_slug)  # type: ignore[attr-defined]
            .first()
        )
    rel = _get_release_by_id(db, id_or_slug)
    if rel is not None:
        return rel
    return (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .options(
            joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
            joinedload(db_models.Release.boxset),  # type: ignore
        )
        .filter(crud.models.Release.slug == id_or_slug)  # type: ignore[attr-defined]
        .first()
    )


def _build_discs_with_job_status(discs) -> List[DiscWithJobStatus]:
    """Build list of DiscWithJobStatus (disc + full JobStatus) for workflow postprocess/transfer."""
    result: List[DiscWithJobStatus] = []
    for disc in discs:
        latest_job = (
            max(disc.jobs, key=lambda j: j.created_at.timestamp() if j.created_at else 0.0)
            if getattr(disc, "jobs", None)
            else None
        )
        job_status = _build_job_status(latest_job) if latest_job else None
        result.append(
            DiscWithJobStatus(
                disc_id=str(disc.id),
                disc_number=disc.disc_number,
                discdb_disc_num=getattr(disc, "discdb_disc_num", None),
                disc_name=disc.disc_name,
                disc_format=disc.format,
                job_status=job_status,
            )
        )
    return result


def _release_summary(rel, db: Session) -> ReleaseSummary:
    discs = rel.discs or []
    discdb_discs = _extract_release_discs_from_jobs(discs)
    total = max(len(discs), len(discdb_discs))
    resolution = getattr(rel, "resolution", None)
    release_year = getattr(rel, "release_year", None)
    production_year = getattr(rel, "production_year", None)
    completed = 0
    finalized = 0
    for d in discs:
        latest_job = sorted(d.jobs, key=lambda j: j.created_at)[-1] if d.jobs else None
        payload = getattr(latest_job, "disc_payload", None) or {}
        resolution = resolution or payload.get("resolution")
        release_year = release_year or payload.get("release_year")
        production_year = production_year or payload.get("production_year") or payload.get("original_year")
        if getattr(d, "finalized", False) or d.finalize_result:
            finalized += 1
        if latest_job and latest_job.job_status == "completed":
            completed += 1
    
    # Get movie data
    movie = None
    movie_id = None
    if hasattr(rel, "movie") and rel.movie:
        movie = MovieSummary(
            id=rel.movie.id,
            name=rel.movie.name,
            production_year=rel.movie.production_year,
            tmdb_id=rel.movie.tmdb_id,
            tmdb_type=rel.movie.tmdb_type,
            cover_url=rel.movie.cover_url,
            cover_path=rel.movie.cover_path,
        )
        movie_id = rel.movie.id
    elif hasattr(rel, "movie_id") and rel.movie_id:
        movie_id = rel.movie_id
    
    # Prefer production_year from movie if available
    if movie and movie.production_year:
        production_year = movie.production_year
    
    # Get boxset_id and boxset_slug if release is linked to a boxset
    boxset_id = getattr(rel, "boxset_id", None)
    boxset_slug = None
    boxset = None
    if hasattr(rel, "boxset") and rel.boxset:
        boxset = rel.boxset
        if boxset:
            boxset_slug = boxset.slug
    
    # Always use boxset values if release is part of boxset (boxset is authoritative)
    # for shared metadata. The display NAME is the exception — for a boxset
    # child we want the underlying movie's title, not the boxset's name,
    # otherwise every release in a boxset reads "Harry Potter 8-Film
    # Collection (year)" instead of "Harry Potter and the Goblet of Fire
    # (year)" etc (#597). The edition string (rel.name) is surfaced via
    # release_name for callers that need it.
    release_name: Optional[str] = None
    if boxset:
        upc = boxset.upc or rel.upc
        asin = boxset.asin or rel.asin
        cover_front_url = boxset.cover_front_url or rel.cover_front_url
        cover_back_url = boxset.cover_back_url or rel.cover_back_url
        if boxset.year:
            release_year = boxset.year
        slug = boxset.slug or rel.slug
        movie_name = rel.movie.name if rel.movie else None
        name = movie_name or rel.name or boxset.name
        release_name = rel.name
    else:
        upc = rel.upc
        asin = rel.asin
        cover_front_url = rel.cover_front_url
        cover_back_url = rel.cover_back_url
        slug = rel.slug
        name = rel.name

    lr = crud.release_link_ready(db, rel)
    miss = crud.release_missing_required_field_keys(db, rel)
    
    return ReleaseSummary(
        id=rel.id,
        slug=slug,
        type=rel.type,
        name=name,
        release_name=release_name,
        movie_id=movie_id,
        movie=movie,
        tmdb_id=rel.movie.tmdb_id if rel.movie else None,
        upc=upc,
        asin=asin,
        cover_front_url=cover_front_url,
        cover_back_url=cover_back_url,
        # title_cover_url removed from Release - should be on Disc
        finalize_state=rel.finalize_state,
        finalized=bool(getattr(rel, "finalized", False)),
        finalized_at=getattr(rel, "finalized_at", None),
        total_discs=total,
        completed_discs=completed,
        finalized_discs=finalized,
        resolution=resolution,
        release_year=release_year,
        original_year=production_year,
        production_year=production_year,
        discdb_hit=bool(discdb_discs) or None,
        boxset_id=boxset_id,
        boxset_slug=boxset_slug,
        release_link_ready=lr,
        release_missing_required_fields=[] if lr else miss,
        modified=bool(getattr(rel, "modified", False)),
    )


def _boxset_summary(
    boxset: db_models.Boxset, db: Session, release_count: int | None = None
) -> BoxsetSummary:
    """Single boxset row for list/search with link-readiness (workflow complete-metadata UI)."""
    from core.release_link_validation import boxset_missing_field_keys

    if release_count is None:
        release_count = (
            db.query(db_models.Release)
            .filter(db_models.Release.boxset_id == boxset.id)
            .count()
        )
    miss = boxset_missing_field_keys(boxset)
    lr = len(miss) == 0
    return BoxsetSummary(
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
        modified=bool(getattr(boxset, "modified", False)),
        boxset_link_ready=lr,
        boxset_missing_required_fields=[] if lr else miss,
    )


def _extract_release_discs_from_jobs(discs) -> list:
    """
    Best-effort: pull the release_discs payload from the most recent job attached
    to any disc in this release so we can expose DiscDB discs (including those not ripped yet).
    """
    for d in discs or []:
        if not d.jobs:
            continue
        for job in sorted(d.jobs, key=lambda j: j.created_at, reverse=True):
            payload = getattr(job, "disc_payload", None) or {}
            release_discs = payload.get("release_discs")
            if release_discs:
                return release_discs
    return []


def _normalize_streams(value):
    """
    Accept legacy array or dict payloads for streams and best-effort parse json strings.
    """
    if value is None:
        return None
    if isinstance(value, (list, dict)):
        return value
    try:
        parsed = json.loads(value)
        if isinstance(parsed, (list, dict)):
            return parsed
    except Exception:
        pass
    return value


def _disc_record(disc, db: Session | None = None) -> DiscRecord:
    title_streams: list[TitleStreamRecord] = []
    titles = []
    for tr in getattr(disc, "title_streams", []) or []:
        try:
            title_streams.append(
                TitleStreamRecord(
                    id=str(tr.id),
                    disc_id=str(tr.disc_id),
                    title_id=getattr(tr, "title_id", None),
                    stream_index=getattr(tr, "stream_index", None),
                    stream_type=getattr(tr, "stream_type", None),
                    audio_type=getattr(tr, "audio_type", None),
                    language_code=getattr(tr, "language_code", None),
                    language=getattr(tr, "language", None),
                    codec_short=getattr(tr, "codec_short", None),
                    codec_hint=getattr(tr, "codec_hint", None),
                    name=getattr(tr, "name", None),
                    bitrate=getattr(tr, "bitrate", None),
                    channels=getattr(tr, "channels", None),
                    sample_rate=getattr(tr, "sample_rate", None),
                    bit_depth=getattr(tr, "bit_depth", None),
                    resolution=getattr(tr, "resolution", None),
                    aspect_ratio=getattr(tr, "aspect_ratio", None),
                    frame_rate=getattr(tr, "frame_rate", None),
                    reference_frames=getattr(tr, "reference_frames", None),
                    description=getattr(tr, "description", None),
                    info=getattr(tr, "info", None),
                    duration_seconds=getattr(tr, "duration_seconds", None),
                    flag=getattr(tr, "flag", None),
                    default=getattr(tr, "default", None),
                    layout=getattr(tr, "layout", None),
                    title=getattr(tr, "title", None),
                    note=getattr(tr, "note", None),
                    duration=getattr(tr, "duration", None),
                    size=getattr(tr, "size", None),
                    streams=_normalize_streams(getattr(tr, "streams", None)),
                    content=getattr(tr, "content", True),
                    order_index=getattr(tr, "order_index", None),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive logging only
            log.warning("Failed to serialize title stream %s: %s", getattr(tr, "id", None), exc)
            continue
    for t in getattr(disc, "titles", []) or []:
        try:
            titles.append(
                DiscTitleRecord(
                    id=str(t.id),
                    disc_id=str(t.disc_id),
                    title_id=None,
                    index=getattr(t, "index", None),
                    order_index=getattr(t, "order_index", None),
                    comment=getattr(t, "comment", None),
                    source_file=getattr(t, "source_file", None),
                    segment_map=getattr(t, "segment_map", None),
                    duration=getattr(t, "duration", None),
                    duration_raw=getattr(t, "duration_raw", None),
                    size=getattr(t, "size", None),
                    display_size=getattr(t, "display_size", None),
                    description=getattr(t, "description", None),
                    title=getattr(t, "title", None),
                    edition=getattr(t, "edition", None),
                    type=_normalize_title_type(getattr(t, "type", None)),
                    season=getattr(t, "season", None),
                    episode=getattr(t, "episode", None),
                    # #600: chapters / streams / detection_flags / metadata_scan
                    # are deferred by `load_only` and unused by the drawer.
                    # Pass `None` literally so we don't trigger one-per-row
                    # lazy SELECTs that re-introduce the N+1 we're fixing.
                    chapters=None,
                    streams=None,
                    content=getattr(t, "content", True),
                    cover_url=getattr(t, "cover_url", None),
                    language_code=getattr(t, "language_code", None),
                    language=getattr(t, "language", None),
                    detection_flags=None,
                    detection_confidence=getattr(t, "detection_confidence", None),
                    detection_warning=getattr(t, "detection_warning", None),
                    # #607: file_path / file_path_stage are eager-loaded in
                    # `_disc_record_load_options` (releases.py:2388-2389) so the
                    # Library disc drawer can render the "At destination" /
                    # "In transient" indicator. They were declared on the
                    # DiscTitleRecord schema (schemas.py:1345-1346) and loaded
                    # by SQLAlchemy but never serialized — the drawer therefore
                    # rendered "Path unknown" for every row regardless of the
                    # stage actually recorded in the DB. Pass them through.
                    file_path=getattr(t, "file_path", None),
                    file_path_stage=getattr(t, "file_path_stage", None),
                )
            )
        except Exception as exc:  # pragma: no cover - defensive logging only
            log.warning("Failed to serialize disc title %s: %s", getattr(t, "id", None), exc)
            continue
    release = getattr(disc, "release", None)
    return DiscRecord(
        id=str(disc.id),
        content_hash=disc.content_hash,
        release_id=disc.release_id,
        release_slug=release.slug if release else None,
        disc_number=disc.disc_number,
        discdb_disc_num=getattr(disc, "discdb_disc_num", None),
        disc_slug=disc.disc_slug,
        disc_name=disc.disc_name,
        format=disc.format,
        finalized=bool(getattr(disc, "finalized", False)),
        finalized_at=getattr(disc, "finalized_at", None),
        artifacts=getattr(disc, "artifacts", None),
        finalize_result=getattr(disc, "finalize_result", None),
        title_streams=title_streams,
        titles=titles,
    )


def _release_record(rel) -> ReleaseRecord:
    # Get movie data
    movie = None
    movie_id = None
    if hasattr(rel, "movie") and rel.movie:
        movie = MovieRecord(
            id=rel.movie.id,
            name=rel.movie.name,
            production_year=rel.movie.production_year,
            tmdb_id=rel.movie.tmdb_id,
            tmdb_type=rel.movie.tmdb_type,
            cover_url=rel.movie.cover_url,
            cover_path=rel.movie.cover_path,
            created_at=rel.movie.created_at,
            updated_at=rel.movie.updated_at,
        )
        movie_id = rel.movie.id
    elif hasattr(rel, "movie_id") and rel.movie_id:
        movie_id = rel.movie_id
    
    # Prefer production_year from movie if available
    production_year = getattr(rel, "production_year", None)
    if movie and movie.production_year:
        production_year = movie.production_year
    
    # Get boxset data and populate fields from boxset if release is linked
    boxset = None
    if hasattr(rel, "boxset") and rel.boxset:
        boxset = rel.boxset
    
    # Always use boxset values if release is part of boxset (boxset is authoritative)
    # Otherwise use release values
    boxset_id = getattr(rel, "boxset_id", None)
    boxset_slug = boxset.slug if boxset else None
    
    if boxset:
        upc = boxset.upc or rel.upc
        asin = boxset.asin or rel.asin
        cover_front_url = boxset.cover_front_url or rel.cover_front_url
        cover_back_url = boxset.cover_back_url or rel.cover_back_url
        release_year = boxset.year or getattr(rel, "release_year", None)
        # Use boxset slug and name if available
        slug = boxset.slug or rel.slug
        name = boxset.name or rel.name
    else:
        upc = rel.upc
        asin = rel.asin
        cover_front_url = rel.cover_front_url
        cover_back_url = rel.cover_back_url
        release_year = getattr(rel, "release_year", None)
        slug = rel.slug
        name = rel.name
    
    return ReleaseRecord(
        id=str(rel.id),
        slug=slug,
        type=rel.type,
        name=name,
        movie_id=movie_id,
        movie=movie,
        tmdb_id=rel.movie.tmdb_id if rel.movie else None,
        upc=upc,
        asin=asin,
        cover_front_url=cover_front_url,
        cover_back_url=cover_back_url,
        # title_cover_url removed from Release - should be on Disc
        boxset_id=boxset_id,
        boxset_slug=boxset_slug,
        finalized=bool(getattr(rel, "finalized", False)),
        finalized_at=getattr(rel, "finalized_at", None),
        release_year=release_year,
        production_year=production_year,
        discs=[_disc_record(d) for d in rel.discs or []],
    )


def _ensure_not_finalized(entity, label: str):
    if getattr(entity, "finalized", False):
        raise HTTPException(status_code=400, detail=f"{label} is finalized and cannot be modified")


@router.get("", response_model=List[ReleaseSummary])
def list_releases(movie_id: str | None = None, db: Session = Depends(get_db)):
    """List all releases, optionally filtered by movie_id."""
    query = (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .options(
            joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
            joinedload(db_models.Release.boxset),  # type: ignore
            selectinload(db_models.Release.discs).selectinload(db_models.Disc.jobs),  # type: ignore[attr-defined]
        )
    )

    # Filter by movie_id if provided
    if movie_id:
        query = query.filter(crud.models.Release.movie_id == movie_id)  # type: ignore[attr-defined]

    # Exclude releases with "pending" slug (these are temporary releases created before user selects/creates a release)
    query = query.filter(
        crud.models.Release.slug != "pending",  # type: ignore[attr-defined]
        ~crud.models.Release.slug.like("pending-%")  # type: ignore[attr-defined]
    )

    releases = query.order_by(crud.models.Release.updated_at.desc()).all()
    # Use shared summary builder so list matches /releases/search (release_link_ready, missing fields, boxset merge).
    return [_release_summary(rel, db) for rel in releases]


# Boxset endpoints

@router.get("/find-by-movie-boxset", response_model=ReleaseSummary | None)
def find_release_by_movie_boxset(movie_id: str, boxset_id: str, db: Session = Depends(get_db)):
    """
    Find a release by movie_id and boxset_id combination.
    Only returns releases that are already linked to this boxset.
    Does NOT fall back to standalone releases or link them automatically.
    Each movie in a boxset needs its own release.
    """
    # Only look for release with both movie_id and boxset_id
    # Do NOT fall back to standalone releases - each movie in a boxset needs its own release
    release = (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .filter(
            crud.models.Release.movie_id == movie_id,  # type: ignore
            crud.models.Release.boxset_id == boxset_id,  # type: ignore
        )
        .options(
            joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
            joinedload(db_models.Release.boxset),  # type: ignore
        )
        .first()
    )
    
    if not release:
        return None
    return _release_summary(release, db)

@router.get("/boxsets", response_model=List[BoxsetSummary])
def list_boxsets(finalized: bool | None = None, db: Session = Depends(get_db)):
    """List all boxsets, optionally filtered by finalized status."""
    boxsets = crud.list_boxsets(db, finalized=finalized)
    return [_boxset_summary(boxset, db) for boxset in boxsets]


@router.get("/boxsets/search", response_model=List[BoxsetSummary])
def search_boxsets(
    q: str = Query("", description="Search term (min 3 chars)"),
    limit: int = Query(20, le=50, description="Max results"),
    db: Session = Depends(get_db),
):
    """Search boxsets by name. For combobox search-as-you-type (≥3 chars)."""
    if len(q) < 3:
        return []
    boxsets = (
        db.query(db_models.Boxset)
        .filter(db_models.Boxset.name.ilike(f"%{q}%"))
        .order_by(db_models.Boxset.name)
        .limit(limit)
        .all()
    )
    return [_boxset_summary(b, db) for b in boxsets]


@router.get("/search", response_model=List[ReleaseSummary])
def search_releases(
    q: str = Query("", description="Search term (min 3 chars for name search)"),
    movie_id: str | None = Query(None, description="Filter by movie ID"),
    limit: int = Query(20, le=50, description="Max results"),
    db: Session = Depends(get_db),
):
    """Search releases by name or movie. For combobox search-as-you-type."""
    if len(q) < 3 and not movie_id:
        return []
    query = db.query(db_models.Release).options(
        joinedload(db_models.Release.movie),
        joinedload(db_models.Release.boxset),
    )
    if movie_id:
        query = query.filter(db_models.Release.movie_id == movie_id)
    if q and len(q) >= 3:
        query = query.filter(
            or_(
                db_models.Release.name.ilike(f"%{q}%"),
                db_models.Release.slug.ilike(f"%{q}%"),
            )
        )
    releases = query.order_by(db_models.Release.updated_at.desc()).limit(limit).all()
    return [_release_summary(r, db) for r in releases]


@router.get("/boxsets/{boxset_id}", response_model=BoxsetRecord)
def get_boxset(boxset_id: str, db: Session = Depends(get_db)):
    """Get boxset details with releases."""
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        raise HTTPException(404, detail="Boxset not found")
    
    # Get linked releases (ordered by creation date since we no longer have disc_index)
    releases_query = db.query(db_models.Release).options(
        joinedload(db_models.Release.movie)
    ).filter(db_models.Release.boxset_id == boxset.id).order_by(db_models.Release.created_at)
    
    releases = []
    for release in releases_query.all():
        releases.append(ReleaseSummary(
                id=release.id,
                slug=release.slug,
                type=release.type,
                name=release.name,
                movie_id=release.movie_id,
                movie=MovieSummary(
                    id=release.movie.id,
                    name=release.movie.name,
                    production_year=release.movie.production_year,
                    tmdb_id=release.movie.tmdb_id,
                    tmdb_type=release.movie.tmdb_type,
                    cover_url=release.movie.cover_url,
                    cover_path=release.movie.cover_path,
                ) if release.movie else None,
                tmdb_id=release.movie.tmdb_id if release.movie else None,
                upc=release.upc,
                asin=release.asin,
                cover_front_url=release.cover_front_url,
                cover_back_url=release.cover_back_url,
                finalize_state=release.finalize_state,
                finalized=release.finalized,
                finalized_at=release.finalized_at,
                total_discs=len(release.discs) if release.discs else 0,
                completed_discs=len([d for d in (release.discs or []) if d.finalize_result]) if release.discs else 0,
                finalized_discs=len([d for d in (release.discs or []) if d.finalized]) if release.discs else 0,
                release_year=release.release_year,
                production_year=release.movie.production_year if release.movie else None,
            ))
    
    return BoxsetRecord(
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
        finalize_result=boxset.finalize_result,
        releases=releases,
        created_at=boxset.created_at,
        updated_at=boxset.updated_at,
    )


@router.get("/boxsets/{boxset_id}/full", response_model=BoxsetFullResponse)
def get_boxset_full(boxset_id: str, db: Session = Depends(get_db)):
    """Return boxset metadata and all discs (across releases) with full JobStatus in one call."""
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        raise HTTPException(404, detail="Boxset not found")
    release_ids = [
        r.id
        for r in db.query(db_models.Release).filter(db_models.Release.boxset_id == boxset_id).all()
    ]
    if not release_ids:
        return BoxsetFullResponse(
            id=boxset.id,
            slug=boxset.slug,
            name=boxset.name,
            year=boxset.year,
            cover_url=boxset.cover_front_url or boxset.cover_back_url,
            discs=[],
        )
    discs = (
        db.query(db_models.Disc)
        .options(joinedload(db_models.Disc.jobs))
        .filter(db_models.Disc.release_id.in_(release_ids))
        .all()
    )
    discs_with_status = _build_discs_with_job_status(discs)
    return BoxsetFullResponse(
        id=boxset.id,
        slug=boxset.slug,
        name=boxset.name,
        year=boxset.year,
        cover_url=boxset.cover_front_url or boxset.cover_back_url,
        discs=discs_with_status,
    )


@router.post("/boxsets", response_model=BoxsetSummary)
def create_boxset(payload: BoxsetCreate, db: Session = Depends(get_db)):
    """Create a new boxset."""
    _log_rb(
        "POST /releases/boxsets start",
        name_preview=(payload.name or "")[:80],
        year=payload.year,
    )
    # Validate required fields and formats
    if not payload.name or not payload.name.strip():
        _log_rb("POST /releases/boxsets failed", reason="validation", field="name")
        raise HTTPException(400, detail="Boxset name is required")
    
    if not _validate_release_year(payload.year):
        _log_rb("POST /releases/boxsets failed", reason="validation", field="year")
        raise HTTPException(400, detail="Boxset year must be a 4-digit number (1000-9999)")
    
    if not _validate_upc(payload.upc):
        _log_rb("POST /releases/boxsets failed", reason="validation", field="upc")
        raise HTTPException(400, detail="UPC must be exactly 12 numeric digits")
    
    if not _validate_cover_url(payload.cover_front_url):
        _log_rb("POST /releases/boxsets failed", reason="validation", field="cover_front_url")
        raise HTTPException(400, detail="Front cover URL must be a valid http:// or https:// URL")
    
    # Generate slug from name
    slug = slugify(payload.name)
    if not slug:
        _log_rb("POST /releases/boxsets failed", reason="validation", field="slug")
        raise HTTPException(400, detail="Boxset name is required")
    
    # Check if slug already exists
    existing = crud.get_boxset_by_slug(db, slug)
    if existing:
        _log_rb(
            "POST /releases/boxsets failed",
            reason="slug_conflict",
            slug=slug,
            existing_boxset_id=existing.id,
        )
        raise HTTPException(409, detail=f"Boxset with slug '{slug}' already exists")
    
    boxset = crud.get_or_create_boxset(db, {
        "slug": slug,
        "name": payload.name,
        "title": payload.title or payload.name,
        "sort_title": payload.sort_title,
        "year": payload.year,
        "upc": payload.upc,
        "asin": payload.asin,
        "locale": payload.locale,
        "region_code": payload.region_code,
        "cover_front_url": payload.cover_front_url,
        "cover_back_url": payload.cover_back_url,
        "release_date": payload.release_date,
    })
    
    if not boxset:
        _log_rb("POST /releases/boxsets failed", reason="get_or_create_boxset_empty")
        raise HTTPException(400, detail="Failed to create boxset")
    
    db.commit()
    db.refresh(boxset)
    
    _invalidate_options()
    _log_rb("POST /releases/boxsets done", boxset_id=boxset.id, slug=boxset.slug)
    return _boxset_summary(boxset, db, release_count=0)


@router.patch("/boxsets/{boxset_id}", response_model=BoxsetSummary)
def update_boxset(boxset_id: str, payload: BoxsetUpdate, db: Session = Depends(get_db)):
    """Update boxset metadata and propagate to all linked releases."""
    from core.release_link_validation import boxset_missing_field_keys, normalize_gtin_from_discdb

    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        _log_rb("PATCH /releases/boxsets/{id} failed", boxset_id=boxset_id, reason="not_found")
        raise HTTPException(404, detail="Boxset not found")

    update_dump = payload.model_dump(exclude_unset=True)
    _log_rb(
        "PATCH /releases/boxsets/{id} start",
        boxset_id=boxset_id,
        fields=sorted(update_dump.keys()),
    )
    
    missing_before = boxset_missing_field_keys(boxset)
    update_data = update_dump
    if "upc" in update_data:
        update_data["upc"] = normalize_gtin_from_discdb(update_data.get("upc"))
    boxset = crud.update_boxset_metadata(db, boxset, update_data)
    db.flush()
    db.refresh(boxset)
    missing_after = boxset_missing_field_keys(boxset)
    if missing_before and not missing_after:
        boxset.modified = True
    db.commit()
    db.refresh(boxset)
    _invalidate_options()

    _log_rb(
        "PATCH /releases/boxsets/{id} done",
        boxset_id=boxset_id,
        missing_before=list(missing_before) if missing_before else None,
        missing_after=list(missing_after) if missing_after else None,
    )
    return _boxset_summary(boxset, db)


@router.post("/boxsets/{boxset_id}/releases/{release_id}")
def add_release_to_boxset(boxset_id: str, release_id: str, db: Session = Depends(get_db)):
    """Add a release to a boxset."""
    _log_rb(
        "POST /releases/boxsets/{boxset_id}/releases/{release_id} start",
        boxset_id=boxset_id,
        release_id=release_id,
    )
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        _log_rb("add_release_to_boxset failed", reason="boxset_not_found", boxset_id=boxset_id)
        raise HTTPException(404, detail="Boxset not found")
    
    release = db.query(db_models.Release).filter(db_models.Release.id == release_id).first()
    if not release:
        _log_rb("add_release_to_boxset failed", reason="release_not_found", release_id=release_id)
        raise HTTPException(404, detail="Release not found")

    prior_boxset_id = release.boxset_id
    release = crud.add_release_to_boxset(db, boxset, release)
    db.commit()

    _log_rb(
        "POST /releases/boxsets/.../releases/... done",
        boxset_id=boxset.id,
        release_id=release.id,
        prior_boxset_id=prior_boxset_id,
    )
    return {"boxset_id": boxset.id, "release_id": release.id}


@router.delete("/boxsets/{boxset_id}/releases/{release_id}")
def remove_release_from_boxset(boxset_id: str, release_id: str, db: Session = Depends(get_db)):
    """Remove a release from a boxset."""
    _log_rb(
        "DELETE /releases/boxsets/{boxset_id}/releases/{release_id} start",
        boxset_id=boxset_id,
        release_id=release_id,
    )
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        _log_rb("remove_release_from_boxset failed", reason="boxset_not_found", boxset_id=boxset_id)
        raise HTTPException(404, detail="Boxset not found")
    
    release = db.query(db_models.Release).filter(db_models.Release.id == release_id).first()
    if not release:
        _log_rb("remove_release_from_boxset failed", reason="release_not_found", release_id=release_id)
        raise HTTPException(404, detail="Release not found")
    
    if release.boxset_id != boxset.id:
        _log_rb(
            "remove_release_from_boxset failed",
            reason="release_not_in_boxset",
            boxset_id=boxset_id,
            release_id=release_id,
            release_boxset_id=release.boxset_id,
        )
        raise HTTPException(404, detail="Release not found in boxset")
    
    release.boxset_id = None
    db.flush()
    
    # Cleanup orphaned release if it has no other discs
    from api.crud import cleanup_orphaned_release
    cleanup_orphaned_release(db, release)
    
    db.commit()

    _log_rb(
        "DELETE /releases/boxsets/.../releases/... done",
        boxset_id=boxset_id,
        release_id=release_id,
    )
    return {"removed": True}


@router.delete("/boxsets/{boxset_id}")
def delete_boxset(boxset_id: str, db: Session = Depends(get_db)):
    """Delete a boxset and all linked releases (cascade delete)."""
    _log_rb("DELETE /releases/boxsets/{id} start", boxset_id=boxset_id)
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        _log_rb("DELETE /releases/boxsets/{id} failed", boxset_id=boxset_id, reason="not_found")
        raise HTTPException(404, detail="Boxset not found")
    
    # Get linked releases count before deletion (for response)
    release_count = db.query(db_models.Release).filter(
        db_models.Release.boxset_id == boxset.id
    ).count()
    
    # Cascade delete will handle releases → discs → titles → tracks
    db.delete(boxset)
    db.commit()

    _log_rb(
        "DELETE /releases/boxsets/{id} done",
        boxset_id=boxset_id,
        deleted_releases=release_count,
    )
    return {"deleted_boxset": boxset_id, "deleted_releases": release_count}


@router.post("/boxsets/{boxset_id}/finalize")
def finalize_boxset_endpoint(boxset_id: str, db: Session = Depends(get_db)):
    """Finalize a boxset after all releases are finalized."""
    _log_rb("POST /releases/boxsets/{id}/finalize start", boxset_id=boxset_id)
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        _log_rb("finalize_boxset failed", boxset_id=boxset_id, reason="not_found")
        raise HTTPException(404, detail="Boxset not found")
    
    # Get all linked releases
    releases = db.query(db_models.Release).options(
        joinedload(db_models.Release.movie),
        joinedload(db_models.Release.discs),
    ).filter(db_models.Release.boxset_id == boxset.id).order_by(db_models.Release.created_at).all()
    
    if not releases:
        _log_rb("finalize_boxset failed", boxset_id=boxset_id, reason="no_releases")
        raise HTTPException(400, detail="Boxset has no releases")
    
    if not releases:
        _log_rb("finalize_boxset failed", boxset_id=boxset_id, reason="no_valid_releases")
        raise HTTPException(400, detail="No valid releases found in boxset")

    _log_rb(
        "finalize_boxset invoking discdb_finalize",
        boxset_id=boxset_id,
        release_count=len(releases),
        release_ids=[r.id for r in releases[:20]],
    )
    # Finalize boxset
    result = discdb_finalize.finalize_boxset(boxset, releases, db)
    
    # Update boxset
    boxset.finalized = True
    boxset.finalized_at = datetime.utcnow()
    boxset.finalize_result = result
    db.commit()
    db.refresh(boxset)

    _log_rb(
        "POST /releases/boxsets/{id}/finalize done",
        boxset_id=boxset_id,
        release_count=len(releases),
        metadata_dir=result.get("boxset_dir"),
    )
    return {
        "boxset": BoxsetSummary(
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
            release_count=len(releases),
        ),
        "metadata_dir": result.get("boxset_dir"),
    }


@router.get("/boxsets/{boxset_id}/export")
def export_boxset(boxset_id: str, format: str = "json", db: Session = Depends(get_db)):
    """Export boxset with all linked releases. ZIP includes releases under movie/series/ and boxset under sets/."""
    boxset = crud.get_boxset_by_id(db, boxset_id)
    if not boxset:
        raise HTTPException(404, detail="Boxset not found")
    
    if not boxset.finalized or not boxset.finalize_result:
        raise HTTPException(400, detail="Boxset not finalized; finalize the boxset before export")
    
    # Get all linked releases (ordered by creation date)
    releases = db.query(db_models.Release).options(
        joinedload(db_models.Release.movie)
    ).filter(db_models.Release.boxset_id == boxset.id).order_by(db_models.Release.created_at).all()
    
    if not releases:
        raise HTTPException(400, detail="Boxset has no releases")
    
    # Validate all releases are finalized
    for release in releases:
        if not release.finalized:
            raise HTTPException(400, detail=f"Release {release.slug} is not finalized")
    
    if format.lower() == "zip":
        import tempfile
        
        tmp_dir = get_mkvauto_root() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / f"{boxset.slug}-export.zip"
        
        if zip_path.exists():
            zip_path.unlink()
        
        try:
            # Create a temporary directory to build the export structure
            with tempfile.TemporaryDirectory() as temp_export:
                temp_export_path = Path(temp_export)
                
                # Copy boxset files to sets/ directory
                boxset_dir = Path(boxset.finalize_result.get("boxset_dir") if isinstance(boxset.finalize_result, dict) else "")
                if boxset_dir and boxset_dir.exists():
                    sets_dir = temp_export_path / "sets"
                    sets_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Get boxset folder name (from finalize_result or construct)
                    boxset_folder_name = boxset_dir.name
                    boxset_dest = sets_dir / boxset_folder_name
                    boxset_dest.mkdir(parents=True, exist_ok=True)
                    
                    # Copy all files from boxset directory
                    for item in boxset_dir.iterdir():
                        if item.is_file():
                            shutil.copy2(item, boxset_dest / item.name)
                
                # Copy each release's files to movie/ or series/ directory
                export_root = get_export_root()
                for release in releases:
                    # Find release export directory
                    release_dir = None
                    for disc in release.discs or []:
                        fin = disc.finalize_result or {}
                        if isinstance(fin, dict):
                            rel_dir = fin.get("release_dir")
                            if rel_dir and Path(rel_dir).exists():
                                release_dir = Path(rel_dir)
                                break
                    
                    if not release_dir or not release_dir.exists():
                        logger.warning(f"Release {release.slug} has no export directory, skipping")
                        continue
                    
                    # Determine release type (movie or series)
                    rel_type = (release.type or "movie").strip().lower() or "movie"
                    type_dir = temp_export_path / rel_type
                    type_dir.mkdir(parents=True, exist_ok=True)
                    
                    # Find the Title (Year) directory level
                    # release_dir is typically: export/movie/Title (Year)/release-slug/
                    current = release_dir
                    title_dir = None
                    while current != export_root.parent and current != Path("/"):
                        if current.parent.parent == export_root:
                            # This is the Title (Year) directory
                            title_dir = current
                            break
                        current = current.parent
                    
                    if title_dir:
                        # Copy the entire Title (Year) directory structure
                        title_dest = type_dir / title_dir.name
                        if title_dest.exists():
                            # Merge if already exists (multiple releases for same movie)
                            for item in title_dir.iterdir():
                                if item.is_dir():
                                    shutil.copytree(item, title_dest / item.name, dirs_exist_ok=True)
                                else:
                                    shutil.copy2(item, title_dest / item.name)
                        else:
                            shutil.copytree(title_dir, title_dest, dirs_exist_ok=True)
                    else:
                        # Fallback: copy release directory directly
                        release_dest = type_dir / release.slug
                        if release_dest.exists():
                            for item in release_dir.iterdir():
                                if item.is_file():
                                    shutil.copy2(item, release_dest / item.name)
                        else:
                            shutil.copytree(release_dir, release_dest, dirs_exist_ok=True)
                
                # Create ZIP from the temporary export structure
                shutil.make_archive(str(zip_path.with_suffix('')), 'zip', root_dir=temp_export_path)
        
        except Exception as exc:
            logger.exception("Failed to build boxset export zip")
            raise HTTPException(500, detail=f"Failed to build export zip: {exc}") from exc
        
        from fastapi.responses import FileResponse
        return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")
    
    # Return JSON
    boxset_json_path = Path(boxset.finalize_result.get("boxset_json") if isinstance(boxset.finalize_result, dict) else "")
    if not boxset_json_path or not boxset_json_path.exists():
        raise HTTPException(404, detail="boxset.json not found")
    
    with boxset_json_path.open("r", encoding="utf-8") as f:
        boxset_json = json.load(f)
    
    return JSONResponse(boxset_json)


@router.get("/library/page")
def get_library_page(
    cursor: str | None = Query(None, description="ISO timestamp cursor for pagination (updated_at)"),
    limit: int = Query(20, le=50, description="Items per page"),
    search: str | None = Query(None, description="Search filter (movie name or release name)"),
    tab: str = Query("all", description="Tab filter: all, movies, series, boxsets"),
    db: Session = Depends(get_db),
):
    """
    Paginated library for infinite scroll.
    Returns releases with discs, grouped by tab. Cursor-based pagination by updated_at descending.
    """
    from datetime import datetime as dt
    from api.schemas import LibraryPageResponse

    query = (
        db.query(crud.models.Release)
        .options(
            joinedload(db_models.Release.movie),
            joinedload(db_models.Release.boxset),
            # selectinload (not joinedload) on the collection legs: chaining two
            # joinedloads off Release.discs blows out into a cartesian against
            # both jobs and titles. With ~60 titles/disc avg (max 325) and 5
            # JSON columns on disc_titles, ORM hydration of the dupe-heavy
            # result pegged uvicorn until OOM. selectinload runs separate
            # WHERE id IN (...) queries — bounded and trivial to plan.
            # #530: discs.disc_info is the raw scan cache (5.8MB of the
            # table's 5.9MB on real data) and nothing on this page reads it
            # — fetching it made the discs selectin the single slowest query
            # (~1.7s). Load only what the summary builders touch.
            selectinload(db_models.Release.discs).load_only(
                db_models.Disc.id,
                db_models.Disc.content_hash,
                db_models.Disc.release_id,
                db_models.Disc.disc_number,
                db_models.Disc.discdb_disc_num,
                db_models.Disc.disc_slug,
                db_models.Disc.disc_name,
                db_models.Disc.format,
                db_models.Disc.finalized,
                db_models.Disc.finalized_at,
                db_models.Disc.finalize_result,
            ),
            selectinload(db_models.Release.discs).selectinload(db_models.Disc.jobs),
            # #530: the page only needs a per-disc title COUNT (the drawer
            # fetches its own DiscRecord on open), but the rows still back
            # label_present/title_count — load_only keeps hydration to the
            # scalar columns and skips the heavy JSON ones (metadata_scan,
            # chapters, streams, detection_flags ≈ 5.5MB table-wide), which
            # dominated this request at ~4s. Field list = the TitleSummary
            # projection + _build_title_summaries sort keys, so the shared
            # builder stays correct for callers that DO emit titles.
            selectinload(db_models.Release.discs)
            .selectinload(db_models.Disc.titles)
            .load_only(
                db_models.DiscTitle.id,
                db_models.DiscTitle.disc_id,
                db_models.DiscTitle.title,
                db_models.DiscTitle.type,
                db_models.DiscTitle.season,
                db_models.DiscTitle.episode,
                db_models.DiscTitle.edition,
                db_models.DiscTitle.description,
                db_models.DiscTitle.duration,
                db_models.DiscTitle.size,
                db_models.DiscTitle.mkv_size,
                db_models.DiscTitle.file_path,
                db_models.DiscTitle.file_path_stage,
                db_models.DiscTitle.title_seq,
                db_models.DiscTitle.active,
                db_models.DiscTitle.order_index,
                db_models.DiscTitle.index,
                db_models.DiscTitle.created_at,
            ),
        )
        .filter(
            crud.models.Release.slug != "pending",
            ~crud.models.Release.slug.like("pending-%"),
        )
    )

    # Tab filter
    if tab == "movies":
        query = query.filter(db_models.Release.type == "movie")
    elif tab == "series":
        query = query.filter(db_models.Release.type.in_(["series", "tv"]))
    elif tab == "boxsets":
        query = query.filter(db_models.Release.boxset_id.isnot(None))

    # Search filter
    if search and len(search) >= 2:
        query = query.outerjoin(db_models.Release.movie).filter(
            or_(
                db_models.Movie.name.ilike(f"%{search}%"),
                db_models.Release.name.ilike(f"%{search}%"),
                db_models.Release.slug.ilike(f"%{search}%"),
            )
        )

    # Cursor-based pagination
    if cursor:
        try:
            cursor_dt = dt.fromisoformat(cursor)
            query = query.filter(db_models.Release.updated_at < cursor_dt)
        except (ValueError, TypeError):
            pass  # Invalid cursor, ignore

    query = query.order_by(db_models.Release.updated_at.desc()).limit(limit + 1)
    results = query.all()

    has_more = len(results) > limit
    results = results[:limit]
    next_cursor = results[-1].updated_at.isoformat() if has_more and results else None

    # #530: one batched lookup of which page discs have TitleStream rows, so
    # label_present never lazy-loads disc.title_streams (which hydrated every
    # stream row's JSON per disc — 37 lazy queries ≈ 4.6s on a 30-release DB).
    page_disc_ids = [str(d.id) for rel in results for d in (getattr(rel, "discs", None) or [])]
    stream_disc_ids: set[str] = set()
    if page_disc_ids:
        stream_disc_ids = {
            str(row[0])
            for row in db.query(db_models.TitleStream.disc_id)
            .filter(db_models.TitleStream.disc_id.in_(page_disc_ids))
            .distinct()
            .all()
        }

    # Build release summaries and disc data (reuse existing logic)
    release_list = []
    release_discs_map = {}
    boxset_ids_seen = set()

    for rel in results:
        summary = _release_summary(rel, db)
        release_list.append(summary)
        # include_titles=False: the page reads only the count; the drawer
        # fetches its own DiscRecord (#530 — was ~1.1MB of inline titles).
        release_discs_map[str(rel.id)] = _build_disc_summaries_for_release(
            rel, include_titles=False, stream_disc_ids=stream_disc_ids
        )
        if rel.boxset_id:
            boxset_ids_seen.add(rel.boxset_id)

    # Build boxset summaries/details only for boxsets referenced in this page
    boxset_summaries = []
    boxset_details_list = []
    if boxset_ids_seen:
        boxsets_in_page = (
            db.query(db_models.Boxset)
            .filter(db_models.Boxset.id.in_(list(boxset_ids_seen)))
            .all()
        )
        for boxset in boxsets_in_page:
            release_count = sum(1 for r in release_list if getattr(r, "boxset_id", None) == boxset.id)
            boxset_summaries.append(
                BoxsetSummary(
                    id=boxset.id, slug=boxset.slug, name=boxset.name,
                    title=boxset.title, sort_title=boxset.sort_title,
                    upc=boxset.upc, asin=boxset.asin, year=boxset.year,
                    locale=boxset.locale, region_code=boxset.region_code,
                    cover_front_url=boxset.cover_front_url, cover_back_url=boxset.cover_back_url,
                    image_url=boxset.image_url, release_date=boxset.release_date,
                    finalized=boxset.finalized, finalized_at=boxset.finalized_at,
                    release_count=release_count,
                )
            )
            releases_in_boxset = [r for r in release_list if getattr(r, "boxset_id", None) == boxset.id]
            boxset_details_list.append(
                BoxsetRecord(
                    id=boxset.id, slug=boxset.slug, name=boxset.name,
                    title=boxset.title, sort_title=boxset.sort_title,
                    upc=boxset.upc, asin=boxset.asin, year=boxset.year,
                    locale=boxset.locale, region_code=boxset.region_code,
                    cover_front_url=boxset.cover_front_url, cover_back_url=boxset.cover_back_url,
                    image_url=getattr(boxset, "image_url", None),
                    release_date=boxset.release_date,
                    finalized=boxset.finalized, finalized_at=boxset.finalized_at,
                    release_count=release_count,
                    releases=releases_in_boxset,
                )
            )

    return LibraryPageResponse(
        items=release_list,
        release_discs=release_discs_map,
        boxsets=boxset_summaries,
        boxset_details=boxset_details_list,
        next_cursor=next_cursor,
        has_more=has_more,
    )


@router.get("/library", response_model=LibraryResponse)
def get_library(db: Session = Depends(get_db)):
    """Pre-structured payload for the Library (History) page: releases with discs, boxsets with details. One-shot load.
    DEPRECATED: Use GET /releases/library/page for paginated loading."""
    query = (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .options(
            joinedload(db_models.Release.movie),  # type: ignore[attr-defined]
            joinedload(db_models.Release.boxset),  # type: ignore
            joinedload(db_models.Release.discs).joinedload(db_models.Disc.jobs),  # type: ignore
        )
        .filter(
            crud.models.Release.slug != "pending",  # type: ignore[attr-defined]
            ~crud.models.Release.slug.like("pending-%"),  # type: ignore[attr-defined]
        )
    )
    releases = query.order_by(crud.models.Release.updated_at.desc()).all()  # type: ignore[attr-defined]
    release_list: List[ReleaseSummary] = []
    release_discs: Dict[str, List[DiscSummary]] = {}
    for rel in releases:
        discs = rel.discs or []
        discdb_discs = _extract_release_discs_from_jobs(discs)
        total = max(len(discs), len(discdb_discs))
        resolution = getattr(rel, "resolution", None)
        release_year = getattr(rel, "release_year", None)
        production_year = getattr(rel, "production_year", None) or getattr(rel, "original_year", None)
        discdb_hit = False
        completed = 0
        finalized = 0
        for d in discs:
            latest_job = sorted(d.jobs, key=lambda j: j.created_at)[-1] if d.jobs else None
            payload = getattr(latest_job, "disc_payload", None) or {}
            resolution = resolution or payload.get("resolution")
            release_year = release_year or payload.get("release_year")
            production_year = production_year or payload.get("production_year") or payload.get("original_year")
            if payload.get("disc_hash") and not payload.get("label_required"):
                discdb_hit = True
            if getattr(d, "finalized", False) or d.finalize_result:
                finalized += 1
            if d.jobs and latest_job and latest_job.job_status == "completed":
                completed += 1
        boxset_id = getattr(rel, "boxset_id", None)
        boxset_slug = getattr(rel.boxset, "slug", None) if getattr(rel, "boxset", None) else None
        boxset = getattr(rel, "boxset", None)
        upc = rel.upc or (boxset.upc if boxset else None)
        asin = rel.asin or (boxset.asin if boxset else None)
        cover_front_url = rel.cover_front_url or (boxset.cover_front_url if boxset else None)
        cover_back_url = rel.cover_back_url or (boxset.cover_back_url if boxset else None)
        if not release_year and boxset and boxset.year:
            release_year = boxset.year
        # Display name: prefer linked movie name for library (e.g. boxset rows show movie title)
        display_name = rel.name
        if getattr(rel, "movie", None) and getattr(rel.movie, "name", None):
            movie_name = (rel.movie.name or "").strip()
            if movie_name:
                display_name = movie_name
        # Production year: prefer linked movie production_year for library
        if getattr(rel, "movie", None) and getattr(rel.movie, "production_year", None) is not None:
            production_year = rel.movie.production_year
        movie_summary = None
        if getattr(rel, "movie", None) and rel.movie:
            # Use release/boxset cover when movie.cover_url is null so History always gets a cover
            movie_cover_url = rel.movie.cover_url or cover_front_url
            movie_summary = MovieSummary(
                id=rel.movie.id,
                name=rel.movie.name or "",
                production_year=rel.movie.production_year,
                tmdb_id=rel.movie.tmdb_id,
                tmdb_type=rel.movie.tmdb_type,
                cover_url=movie_cover_url,
                cover_path=rel.movie.cover_path,
            )
        release_list.append(
            ReleaseSummary(
                id=rel.id,
                slug=rel.slug,
                type=rel.type,
                name=display_name,
                title=display_name,
                release_name=rel.name,
                movie=movie_summary,
                tmdb_id=rel.movie.tmdb_id if rel.movie else None,
                upc=upc,
                asin=asin,
                cover_front_url=cover_front_url,
                cover_back_url=cover_back_url,
                finalize_state=rel.finalize_state,
                finalized=bool(getattr(rel, "finalized", False)),
                finalized_at=getattr(rel, "finalized_at", None),
                total_discs=total,
                completed_discs=completed,
                finalized_discs=finalized,
                resolution=resolution,
                release_year=release_year,
                original_year=production_year,
                production_year=production_year,
                discdb_hit=discdb_hit or None,
                boxset_id=boxset_id,
                boxset_slug=boxset_slug,
            )
        )
        release_discs[str(rel.id)] = _build_disc_summaries_for_release(rel)

    boxsets = crud.list_boxsets(db, finalized=None)
    boxset_summaries: List[BoxsetSummary] = []
    boxset_details_list: List[BoxsetRecord] = []
    for boxset in boxsets:
        release_count = sum(1 for r in release_list if getattr(r, "boxset_id", None) == boxset.id)
        boxset_summaries.append(
            BoxsetSummary(
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
        )
        releases_in_boxset = [r for r in release_list if getattr(r, "boxset_id", None) == boxset.id]
        boxset_details_list.append(
            BoxsetRecord(
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
                finalize_result=getattr(boxset, "finalize_result", None),
                releases=releases_in_boxset,
                created_at=getattr(boxset, "created_at", None),
                updated_at=getattr(boxset, "updated_at", None),
            )
        )
    return LibraryResponse(
        releases=release_list,
        release_discs=release_discs,
        boxsets=boxset_summaries,
        boxset_details=boxset_details_list,
    )


@router.get("/{slug}", response_model=ReleaseSummary)
def get_release(slug: str, db: Session = Depends(get_db)):
    rel = _get_release_by_id(db, slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    return _release_summary(rel, db)


def _patch_disc_ops_internal(
    disc_id: str,
    ops: List[Dict[str, Any]],
    db: Session,
    allow_source_file: bool = True
) -> db_models.Disc:
    """
    Internal function to apply ops to a disc.
    Extracted from patch_disc_ops router function for reuse.
    Returns updated disc record.
    """
    disc = (
        db.query(db_models.Disc)  # type: ignore[attr-defined]
        .options(joinedload(db_models.Disc.release))  # type: ignore[attr-defined]
        .filter(db_models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    release = getattr(disc, "release", None)
    _ensure_not_finalized(disc, "Disc")
    if release:
        _ensure_not_finalized(release, "Release")

    # Track if release_year or release_name changed to trigger slug regeneration
    release_year_changed = False
    release_name_changed = False
    
    # Collect release data from ops to create release if needed
    release_data_from_ops = {}
    # #363 H1 — lazily-resolved active job for the title-patch guard (one
    # query per request, only when a title op is present).
    _OPS_JOB_UNSET = object()
    _ops_active_job: Any = _OPS_JOB_UNSET
    for op in ops:
        # Handle both dict and object formats
        target = op.get("target") if isinstance(op, dict) else getattr(op, "target", None)
        fields = op.get("fields") if isinstance(op, dict) else getattr(op, "fields", {})
        op_id = op.get("id") if isinstance(op, dict) else getattr(op, "id", None)
        
        if target == "release":
            # Collect all release fields (even if release doesn't exist yet)
            for k, v in fields.items():
                if k == "release_slug":
                    # Release slug is auto-generated, ignore if provided
                    continue
                release_data_from_ops[k] = v
            
            # If release exists, apply the updates
            if release:
                # Unlink boxset: clear release.boxset_id and cleanup orphaned boxset if it has no other releases
                if "boxset_id" in fields and fields.get("boxset_id") is None:
                    old_boxset_id = getattr(release, "boxset_id", None)
                    release.boxset_id = None
                    if old_boxset_id:
                        old_boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == old_boxset_id).first()
                        if old_boxset:
                            from api.crud import cleanup_orphaned_boxset
                            cleanup_orphaned_boxset(db, old_boxset)
                for k, v in fields.items():
                    target_field = k
                    if k == "release_name":
                        target_field = "name"
                        release_name_changed = True
                    elif k == "release_slug":
                        # Release slug is auto-generated, ignore if provided
                        continue
                    elif k == "release_year":
                        target_field = "release_year"
                        release_year_changed = True
                    elif k == "boxset_id":
                        # Already handled above for None; for non-None link via add_release_to_boxset
                        # so boxset UPC/cover/slug copy runs before end-of-handler release_link_ready.
                        if v is not None and hasattr(release, "boxset_id"):
                            link_boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == v).first()
                            if link_boxset:
                                crud.add_release_to_boxset(db, link_boxset, release)
                        continue
                    if hasattr(release, target_field):
                        setattr(release, target_field, v)
        elif target == "disc":
            for k, v in fields.items():
                if hasattr(disc, k):
                    setattr(disc, k, v)
            if "release_id" in fields and not fields.get("release_id"):
                # Get old release before unlinking
                old_release_id = disc.release_id
                disc.release_id = None
                disc.disc_number = None
                release = None
                db.flush()  # CRITICAL: Flush so cleanup_orphaned_release sees disc as unlinked
                # Cleanup orphaned release if it has no other discs; then cleanup orphaned boxset if any
                if old_release_id:
                    old_release = db.query(db_models.Release).filter(db_models.Release.id == old_release_id).first()
                    if old_release:
                        from api.crud import cleanup_orphaned_release, cleanup_orphaned_boxset
                        old_boxset_id = getattr(old_release, "boxset_id", None)
                        cleanup_orphaned_release(db, old_release)
                        if old_boxset_id:
                            old_boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == old_boxset_id).first()
                            if old_boxset:
                                cleanup_orphaned_boxset(db, old_boxset)
            else:
                if "disc_slug" in fields:
                    crud.apply_disc_slug_from_label_payload(disc, fields.get("disc_slug"))
                elif "disc_name" in fields:
                    crud.apply_disc_slug_from_label_payload(disc, None)
        elif target == "title":
            if not op_id:
                continue
            # Try to find title by database ID first
            title = (
                db.query(db_models.DiscTitle)  # type: ignore[attr-defined]
                .filter(db_models.DiscTitle.id == op_id, db_models.DiscTitle.disc_id == disc.id)  # type: ignore
                .first()
            )
            # If not found by ID, optionally try to find by source_file (legacy compatibility)
            if not title and allow_source_file:
                title = (
                    db.query(db_models.DiscTitle)  # type: ignore[attr-defined]
                    .filter(db_models.DiscTitle.source_file == op_id, db_models.DiscTitle.disc_id == disc.id)  # type: ignore
                    .order_by(db_models.DiscTitle.index.asc().nulls_last())  # type: ignore
                    .first()
                )
            if not title:
                continue
            incoming_seq = None
            if "title_seq" in fields:
                incoming_seq = fields.pop("title_seq")
            current_seq = getattr(title, "title_seq", 0) or 0
            if incoming_seq is None:
                incoming_seq = current_seq + 1
            if incoming_seq < current_seq:
                continue
            # #363 H1 — same pipeline guard as PATCH /discs/{id}/titles.
            from api.routers.discs import assert_title_patch_allowed, get_active_job_for_disc
            if _ops_active_job is _OPS_JOB_UNSET:
                _ops_active_job = get_active_job_for_disc(db, str(disc.id))
            assert_title_patch_allowed(_ops_active_job, title, set(fields))
            title.title_seq = incoming_seq
            for k, v in fields.items():
                if hasattr(title, k):
                    setattr(title, k, v)
        elif target == "stream":
            if not op_id:
                continue
            stream_row = (
                db.query(db_models.TitleStream)  # type: ignore[attr-defined]
                .filter(db_models.TitleStream.id == op_id, db_models.TitleStream.disc_id == disc.id)  # type: ignore
                .first()
            )
            if not stream_row:
                continue
            for k, v in fields.items():
                if hasattr(stream_row, k):
                    setattr(stream_row, k, v)
        elif target == "label_draft":
            # Unlink movie: when movie_id is **explicitly** cleared (key
            # present with value None), unlink the disc from its release
            # and cleanup the orphaned release/boxset. Crucially, we must
            # NOT fire this branch when `movie_id` is simply absent from
            # `fields` (e.g. a primary_season-only PATCH from
            # `setPrimarySeason`, #536) — `.get(key)` returning None can
            # mean either "absent" or "explicitly cleared", and unlinking
            # on absence drops the disc's release link on any field-scoped
            # mutation (#538).
            if (
                fields
                and isinstance(fields, dict)
                and "movie_id" in fields
                and fields["movie_id"] is None
            ):
                old_release_id = disc.release_id
                if old_release_id:
                    disc.release_id = None
                    disc.disc_number = None
                    release = None
                    db.flush()
                    old_release = db.query(db_models.Release).filter(db_models.Release.id == old_release_id).first()
                    if old_release:
                        from api.crud import cleanup_orphaned_release, cleanup_orphaned_boxset
                        old_boxset_id = getattr(old_release, "boxset_id", None)
                        cleanup_orphaned_release(db, old_release)
                        if old_boxset_id:
                            old_boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == old_boxset_id).first()
                            if old_boxset:
                                cleanup_orphaned_boxset(db, old_boxset)
            # label_draft holds disc-scoped editing state that hasn't been
            # finalized into a Release yet: movie_id, group_type, and
            # primary_season (the disc-card season pick for multi-disc TV
            # releases — #371, persistence added in #536). release/boxset
            # data still lives on Release/Boxset.
            if fields and isinstance(fields, dict):
                existing_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
                merged = {**existing_draft, **fields}
                allowed_keys = {"movie_id", "group_type", "primary_season"}
                disc.label_draft = {k: v for k, v in merged.items() if k in allowed_keys}

    # Auto-populate disc format/name/slug from disc_info if missing
    # This ensures disc metadata is populated from scan data
    if disc.disc_info:
        disc_info = disc.disc_info
        # Auto-populate format if missing
        if not disc.format and (disc_info.get("disc_format") or disc_info.get("format")):
            disc.format = disc_info.get("disc_format") or disc_info.get("format")
        
        # Auto-populate disc_name from format if missing
        if not disc.disc_name:
            disc_name = disc_info.get("disc_name")
            if not disc_name and disc.format:
                disc_name = disc.format
            if disc_name:
                disc.disc_name = disc_name
        
        # Auto-populate disc_slug from disc_name if missing
        if not disc.disc_slug:
            disc_slug = disc_info.get("disc_slug")
            if not disc_slug and disc.disc_name:
                from api.crud import _disc_name_sluggify
                disc_slug = _disc_name_sluggify(disc.disc_name)
            if disc_slug:
                disc.disc_slug = disc_slug

    crud.backfill_disc_slug_if_blank(disc)

    # Create release if needed (when release_year or release_name is provided but no release exists)
    if not release and release_data_from_ops:
        # Check if we have enough data to create a release
        # Also check disc's existing data for movie_id (might be set from previous step)
        movie_id = release_data_from_ops.get("movie_id") or release_data_from_ops.get("film_id")
        # If movie_id not in ops, try to get it from disc's existing release or label_draft
        if not movie_id:
            # Check if disc has a release with a movie_id
            if disc.release_id:
                existing_release = disc.release
                if existing_release and existing_release.movie_id:
                    movie_id = existing_release.movie_id
            # Also check label_draft for movie_id
            if not movie_id and disc.label_draft:
                label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
                movie_id = label_draft.get("movie_id") or label_draft.get("film_id")
        release_year = release_data_from_ops.get("release_year")
        release_name = release_data_from_ops.get("release_name")
        production_year = release_data_from_ops.get("production_year")
        boxset_id = release_data_from_ops.get("boxset_id")
        
        # Logic:
        # - No boxset: require BOTH movie_id AND release_name (wait for user to enter release name)
        # - Has boxset: require movie_id (can use boxset name/slug for release_name if needed)
        has_boxset = boxset_id is not None
        can_create = False
        boxset = None
        
        if has_boxset:
            # With boxset: only need movie_id (we'll use boxset info for release_name if needed)
            can_create = movie_id is not None
            if can_create:
                # Get boxset to use its year for release_year
                boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_id).first()  # type: ignore
                if boxset:
                    # Use boxset year for release_year (boxset release year)
                    if not release_year and boxset.year:
                        release_year = boxset.year
                    # Use boxset name/slug for release_name if not provided
                    if not release_name:
                        release_name = boxset.name or boxset.slug
        else:
            # No boxset: require BOTH movie_id AND release_name
            # Use production_year as fallback for release_year if release_year is not provided (only when no boxset)
            if release_year is None and production_year is not None:
                release_year = production_year
            can_create = movie_id is not None and release_name is not None and release_name.strip() != ""
        
        if can_create:
            # Verify movie exists
            movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
            if movie:
                # For production_year: use movie.production_year (from TMDB) if available, otherwise use from ops
                final_production_year = movie.production_year if movie.production_year else production_year
                
                # Look up existing release by movie_id and boxset_id (NOT by slug)
                # Each movie in a boxset needs its own release
                if boxset_id:
                    # Look for release with matching movie_id AND boxset_id
                    existing = (
                        db.query(db_models.Release)
                        .filter(
                            db_models.Release.movie_id == movie_id,
                            db_models.Release.boxset_id == boxset_id
                        )
                        .first()
                    )
                else:
                    # Look for release with matching movie_id and no boxset_id
                    existing = (
                        db.query(db_models.Release)
                        .filter(
                            db_models.Release.movie_id == movie_id,
                            db_models.Release.boxset_id.is_(None)
                        )
                        .first()
                    )
                
                if existing:
                    # Release exists for this movie_id + boxset_id combination - reuse it
                    release = existing
                    disc.release_id = release.id
                    # Update release metadata if provided
                    if release_name and not release.name:
                        release.name = release_name
                    if release_year and not release.release_year:
                        release.release_year = release_year
                    if release_year:
                        release_year_changed = True
                    if release_name:
                        release_name_changed = True
                else:
                    # No release exists for this movie_id + boxset_id - create new one
                    # Skip slug generation if boxset is linked (will use boxset.slug)
                    desired_slug = "pending"
                    if not boxset_id:
                        # Only generate slug if not in boxset
                        desired_slug = _compute_release_slug(None, {
                            "release_year": release_year,
                            "release_name": release_name,
                        }) or "pending"
                    
                    # If boxset is provided, use boxset.slug instead
                    final_slug = desired_slug
                    if boxset_id:
                        boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_id).first()  # type: ignore
                        if boxset and boxset.slug:
                            final_slug = boxset.slug
                    
                    # Create new release
                    # Copy boxset metadata to release when creating (boxset is authoritative)
                    release_upc = release_data_from_ops.get("upc") or (boxset.upc if boxset else None)
                    release_asin = release_data_from_ops.get("asin") or (boxset.asin if boxset else None)
                    release_cover_front_url = release_data_from_ops.get("cover_front_url") or (boxset.cover_front_url if boxset else None)
                    release_cover_back_url = release_data_from_ops.get("cover_back_url") or (boxset.cover_back_url if boxset else None)
                    
                    release = db_models.Release(
                        slug=final_slug,
                        type=release_data_from_ops.get("group_type") or release_data_from_ops.get("mode") or "movie",
                        name=release_name,
                        movie_id=movie_id,
                        release_year=release_year,  # From boxset.year when boxset is present
                        upc=release_upc,
                        asin=release_asin,
                        cover_front_url=release_cover_front_url,
                        cover_back_url=release_cover_back_url,
                        boxset_id=release_data_from_ops.get("boxset_id"),  # Link to boxset if provided
                    )
                    db.add(release)
                    db.flush()
                    disc.release_id = release.id
                    # Mark that we changed release data
                    if release_year:
                        release_year_changed = True
                    if release_name:
                        release_name_changed = True
    
    # Auto-generate slug when release year or name is updated
    # BUT skip if release is part of boxset (boxset.slug is authoritative)
    if release and (release_year_changed or release_name_changed) and not release.boxset_id:
        desired_slug = _compute_release_slug(release, {
            "release_year": getattr(release, "release_year", None),
            "release_name": getattr(release, "name", None),
        })
        # Update slug from "pending" or if it changed
        if desired_slug and (release.slug == "pending" or desired_slug != release.slug):
            release.slug = desired_slug
    
    # Calculate disc_number if release was just created or linked, and disc_number is not set
    if release and disc.disc_number is None:
        # Calculate disc number when release is assigned
        from api.crud import _next_disc_number
        disc.disc_number = _next_disc_number(db, release, exclude_disc_id=disc.id)
    
    # Normalize disc numbers if requested (e.g., when continuing from boxset/release step)
    recalculate_disc_numbers = release_data_from_ops.get("recalculate_disc_numbers", False)
    if recalculate_disc_numbers and release:
        from api.crud import normalize_disc_numbers_for_release, _next_disc_numbers_all
        normalize_disc_numbers_for_release(db, release, exclude_disc_id=disc.id)
        # Refresh to get updated disc numbers
        db.refresh(release)
        # Recalculate current disc's number after normalization
        # Count all discs (including unfinished) for the current disc's number
        disc.disc_number = _next_disc_numbers_all(db, release, exclude_disc_id=disc.id)
    
    if disc.release_id:
        link_rel = (
            db.query(db_models.Release)
            .options(joinedload(db_models.Release.boxset))
            .filter(db_models.Release.id == disc.release_id)
            .first()
        )
        if link_rel is None:
            raise HTTPException(404, detail="Linked release not found")
        if not crud.release_link_ready(db, link_rel):
            db.rollback()
            raise HTTPException(
                400,
                detail={
                    "error": "release_not_link_ready",
                    "missing": crud.release_missing_required_field_keys(db, link_rel),
                },
            )
        crud.sync_disc_pending_release_metadata(db, disc, link_rel, True)
        crud.sync_disc_label_draft_with_release(disc, link_rel)

    db.commit()
    db.refresh(disc)
    if release:
        db.refresh(release)
    
    # Emit websocket update if title or track was updated
    has_title_or_stream_update = any(op.get("target") in ("title", "stream") for op in ops)
    if has_title_or_stream_update:
        try:
            from api.routers.websockets import _emit_to_disc_workflow
            import asyncio
            
            async def _emit_update():
                try:
                    await _emit_to_disc_workflow(disc_id, changed_fields=['titles'])
                except Exception as exc:
                    log.warning(f"Failed to emit title/stream update notification to websocket for disc {disc_id}: {exc}")
            
            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(_emit_update())
            except RuntimeError:
                # No running loop - try to get app reference
                try:
                    from api.main import _app_instance
                    if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                        loop = _app_instance.state.event_loop
                        asyncio.run_coroutine_threadsafe(_emit_update(), loop)
                except Exception as exc:
                    log.warning(f"Failed to schedule websocket emission for disc {disc_id}: {exc}")
        except Exception as exc:
            log.warning(f"Failed to emit title/stream update to websocket for disc {disc_id}: {exc}")
    
    return disc


@router.patch("/disc/{disc_id}/ops")
def patch_disc_ops(disc_id: str, req: PatchRequest, db: Session = Depends(get_db)):
    """
    Apply fine-grained patches to release/disc/title/stream records without rebuilding title_streams.
    Public endpoint - calls internal function.
    """
    # Convert PatchOp objects to dicts for internal function
    ops_list = []
    for op in req.ops:
        op_dict = {"target": op.target, "fields": op.fields}
        if op.id:
            op_dict["id"] = op.id
        ops_list.append(op_dict)
    disc = _patch_disc_ops_internal(disc_id, ops_list, db)
    return _disc_record(disc)


@router.get("/{slug}/record", response_model=ReleaseRecord)
def get_release_record(slug: str, db: Session = Depends(get_db)):
    rel = _get_release_by_id(db, slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    return _release_record(rel)


@router.get("/{release_id}/full", response_model=ReleaseFullResponse)
def get_release_full(release_id: str, db: Session = Depends(get_db)):
    """Return release metadata and all discs with full JobStatus (post + transfer) in one call."""
    rel = (
        db.query(db_models.Release)
        .options(
            joinedload(db_models.Release.movie),
            joinedload(db_models.Release.discs).joinedload(db_models.Disc.jobs),
        )
        .filter(db_models.Release.id == release_id)
        .first()
    )
    if not rel:
        raise HTTPException(404, detail="Release not found")
    discs = rel.discs or []
    discs_with_status = _build_discs_with_job_status(discs)
    cover_url = (rel.movie.cover_url if rel.movie else None) or getattr(rel, "cover_front_url", None)
    return ReleaseFullResponse(
        id=str(rel.id),
        slug=rel.slug,
        name=rel.name,
        movie_name=rel.movie.name if rel.movie else None,
        production_year=rel.movie.production_year if rel.movie else None,
        release_name=rel.name,
        release_slug=rel.slug,
        cover_url=cover_url,
        discs=discs_with_status,
    )


@router.patch("/{slug}", response_model=ReleaseSummary)
def patch_release(slug: str, req: ReleaseMetadataPatch, db: Session = Depends(get_db)):
    rel = _get_release_by_id(db, slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    _ensure_not_finalized(rel, "Release")
    updates: Dict[str, Any] = {}
    prior_year = getattr(rel, "release_year", None)
    incoming_year = req.release_year if req.release_year is not None else None
    # Release slug is auto-generated, ignore if provided
    # if req.release_slug is not None:
    #     updates["slug"] = req.release_slug
    if req.release_name is not None:
        updates["name"] = req.release_name
    if req.release_year is not None:
        updates["release_year"] = req.release_year
    # tmdb_id removed from Release - use Movie.tmdb_id instead
    if req.upc is not None:
        updates["upc"] = req.upc
    if req.asin is not None:
        updates["asin"] = req.asin
    if req.cover_front_url is not None:
        updates["cover_front_url"] = req.cover_front_url
    if req.cover_back_url is not None:
        updates["cover_back_url"] = req.cover_back_url
    if req.group_type is not None:
        updates["type"] = req.group_type
    if req.boxset_id is not None:
        # Allow setting boxset_id to None to unlink
        updates["boxset_id"] = req.boxset_id if req.boxset_id else None
    # Auto-slug when year is provided (even if unchanged), name changes, or when we still have a pending slug.
    # Use release year as the trigger to generate the slug
    if req.release_year is not None or req.release_name is not None:
        desired_slug = _compute_release_slug(rel, {
            "release_year": incoming_year or getattr(rel, "release_year", None),
            "release_name": req.release_name if req.release_name is not None else getattr(rel, "name", None),
        })
        if desired_slug:
            # Always regenerate slug if year is provided or name changed, or if slug is pending
            if req.release_year is not None or req.release_name is not None or rel.slug.startswith("pending-"):
                updates["slug"] = desired_slug
    if updates:
        db.query(db_models.Release).filter(db_models.Release.id == rel.id).update(updates)
        db.commit()
        db.refresh(rel)
    return _release_summary(rel, db)


def _normalize_tracks_for_summary(raw: Any) -> Optional[List[Dict[str, Any]]]:
    """DiscSummary.tracks must be a list; payloads often store tracks as dict keyed by title id."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return list(raw.values())
    if isinstance(raw, list):
        return raw
    return None


def _build_title_summaries(
    title_rows: Optional[List[Any]],
) -> Optional[List[TitleSummary]]:
    """#380: project DiscTitle rows into the typed TitleSummary subset the
    Library response carries. Returns None when the disc has no titles
    (preserves the existing 'null when no data' contract for Pydantic
    optional fields).

    Sort order matches the GET /discs/{disc_id}/titles endpoint
    (order_index → index → created_at, nulls last) so the Library and the
    Ripper titles step render the same sequence."""
    if not title_rows:
        return None

    def _sort_key(t: Any) -> tuple:
        oi = getattr(t, "order_index", None)
        idx = getattr(t, "index", None)
        created = getattr(t, "created_at", None)
        return (
            (oi is None, oi if oi is not None else 0),
            (idx is None, idx if idx is not None else 0),
            (created is None, created),
        )

    summaries: List[TitleSummary] = []
    for t in sorted(title_rows, key=_sort_key):
        summaries.append(
            TitleSummary(
                title_id=str(t.id),
                title=t.title,
                type=t.type,
                season=t.season,
                episode=t.episode,
                edition=getattr(t, "edition", None),
                description=t.description,
                duration=t.duration,
                size=t.size,
                mkv_size=getattr(t, "mkv_size", None),
                file_path=getattr(t, "file_path", None),
                file_path_stage=getattr(t, "file_path_stage", None),
                title_seq=getattr(t, "title_seq", 0) or 0,
                active=getattr(t, "active", None),
            )
        )
    return summaries or None


def _build_disc_summaries_for_release(
    rel,
    *,
    include_titles: bool = True,
    stream_disc_ids: set[str] | None = None,
) -> List[DiscSummary]:
    """Build DiscSummary list for a release (with discs and jobs loaded). Used by list_release_discs and get_library.

    #530 knobs (both default to the pre-existing behavior so
    list_release_discs / the deprecated get_library are byte-identical):

    - ``include_titles=False`` omits the inline TitleSummary projection and
      ships ``title_count`` only — the Library page reads just the count and
      the drawer fetches its own DiscRecord.
    - ``stream_disc_ids`` (set of disc ids known to have TitleStream rows)
      lets ``label_present`` avoid touching ``disc.title_streams``, which is
      a per-disc lazy load when the caller didn't selectinload it.
    """
    discs = getattr(rel, "discs", None) or []
    discdb_discs = _extract_release_discs_from_jobs(discs)
    results: List[DiscSummary] = []
    for d in discs:
        latest_job = sorted(d.jobs, key=lambda j: j.created_at)[-1] if d.jobs else None
        latest_pipeline = None
        latest_phase = None
        latest_job_updated_at = None
        if latest_job:
            latest_pipeline, latest_phase = _derive_pipeline(latest_job)
            latest_job_updated_at = getattr(latest_job, "updated_at", None)
        per_title = getattr(latest_job, "per_title_progress", None) if latest_job else None
        payload = getattr(latest_job, "disc_payload", None) or {}
        discdb_hit = bool(payload.get("disc_hash")) and not bool(payload.get("label_required"))
        tracks = _normalize_tracks_for_summary(payload.get("tracks"))
        # #380: project the disc's title rows onto the response so callers
        # that need per-title metadata get it without fetching per-disc.
        # #530: the Library page opts out (count only).
        loaded_titles = getattr(d, "titles", None)
        title_summaries = _build_title_summaries(loaded_titles) if include_titles else None
        if stream_disc_ids is not None:
            has_streams = str(d.id) in stream_disc_ids
        else:
            has_streams = bool(getattr(d, "title_streams", None))
        results.append(
            DiscSummary(
                id=d.id,
                content_hash=d.content_hash,
                release_id=rel.id,
                release_slug=rel.slug,
                disc_number=d.disc_number,
                discdb_disc_num=getattr(d, "discdb_disc_num", None),
                disc_slug=d.disc_slug,
                disc_name=d.disc_name,
                format=d.format,
                label_present=bool(has_streams or loaded_titles),
                finalized=bool(getattr(d, "finalized", False) or d.finalize_result),
                finalized_at=getattr(d, "finalized_at", None),
                finalize_result=d.finalize_result,
                latest_job_id=str(latest_job.id) if latest_job else None,
                latest_job_status=latest_job.job_status if latest_job else None,
                scan_state=getattr(latest_job, "scan_state", None) if latest_job else None,
                latest_job_progress=latest_job.rip_progress if latest_job else None,
                latest_pipeline=latest_pipeline,
                latest_phase=latest_phase,
                latest_job_updated_at=latest_job_updated_at,
                transfer_state=getattr(latest_job, "transfer_state", None) if latest_job else None,
                discdb_hit=discdb_hit,
                titles_completed=getattr(latest_job, "titles_completed", None) if latest_job else None,
                total_titles=getattr(latest_job, "total_titles", None) if latest_job else None,
                per_title_progress=per_title or None,
                tracks=tracks,
                titles=title_summaries,
                title_count=len(loaded_titles or []),
            )
        )

    # Append DiscDB discs that we haven't ripped yet so the UI can show upcoming discs/tracks.
    seen_hashes = {d.content_hash for d in results if d.content_hash}
    for idx, disc_info in enumerate(discdb_discs):
        content_hash = disc_info.get("content_hash") or disc_info.get("contentHash") or disc_info.get("slug") or f"discdb-{rel.slug}-{idx+1}"
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        disc_number = disc_info.get("index")
        discdb_phantom_num = None
        try:
            if disc_number is not None:
                discdb_phantom_num = int(disc_number)
                disc_number = discdb_phantom_num + 1
        except Exception:
            pass
        results.append(
            DiscSummary(
                id=None,
                content_hash=str(content_hash),
                release_id=rel.id,
                release_slug=rel.slug,
                disc_number=disc_number,
                discdb_disc_num=discdb_phantom_num,
                disc_slug=disc_info.get("slug"),
                disc_name=disc_info.get("name"),
                format=disc_info.get("format"),
                label_present=False,
                finalized=False,
                latest_job_id=None,
                latest_job_status=None,
                scan_state=None,
                latest_job_progress=None,
                latest_pipeline=None,
                latest_phase=None,
                latest_job_updated_at=None,
                transfer_state=None,
                discdb_hit=True,
                titles_completed=None,
                total_titles=None,
                per_title_progress=None,
                tracks=_normalize_tracks_for_summary(disc_info.get("tracks")),
                finalize_result=None,
            )
        )
    try:
        results.sort(key=lambda item: ((item.disc_number or 10**6), item.content_hash))
    except Exception:
        pass
    return results


@router.get("/{slug}/discs", response_model=List[DiscSummary])
def list_release_discs(slug: str, db: Session = Depends(get_db)):
    rel = _get_release_by_id_or_slug(db, slug, load_discs=True)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    return _build_disc_summaries_for_release(rel)


def _disc_record_load_options() -> tuple:
    """SQLAlchemy `.options(...)` for `GET /releases/disc/{id}` — applies the
    #530 → #532 playbook to a single-disc projection (#600):

    1. `load_only` on the outer Disc skips the multi-MB scan cache
       (`disc_info`) and the label/finalize JSON blobs that the drawer
       never reads.
    2. `selectinload(Disc.titles).load_only(...)` keeps only the scalar
       columns `_disc_record` actually projects to `DiscTitleRecord`,
       dropping `chapters`, `streams`, `metadata_scan`, and
       `detection_flags` — the four JSON columns whose hydration +
       Pydantic-validation cost dominated the request on a 309-title
       disc (30+ s before this trim).
    3. `selectinload(Disc.title_streams)` batches what was previously a
       lazy load fired mid-projection.

    Heavy fields stay `Optional` on `DiscTitleRecord` / `DiscRecord`, so
    they resolve to `None` in the response — no schema break.
    """
    return (
        load_only(
            db_models.Disc.id,
            db_models.Disc.content_hash,
            db_models.Disc.release_id,
            db_models.Disc.disc_number,
            db_models.Disc.discdb_disc_num,
            db_models.Disc.disc_slug,
            db_models.Disc.disc_name,
            db_models.Disc.format,
            db_models.Disc.finalized,
            db_models.Disc.finalized_at,
            db_models.Disc.finalize_result,
            db_models.Disc.artifacts,
        ),
        selectinload(db_models.Disc.titles).load_only(
            db_models.DiscTitle.id,
            db_models.DiscTitle.disc_id,
            db_models.DiscTitle.index,
            db_models.DiscTitle.order_index,
            db_models.DiscTitle.comment,
            db_models.DiscTitle.source_file,
            db_models.DiscTitle.segment_map,
            db_models.DiscTitle.duration,
            db_models.DiscTitle.duration_raw,
            db_models.DiscTitle.size,
            db_models.DiscTitle.display_size,
            db_models.DiscTitle.description,
            db_models.DiscTitle.title,
            db_models.DiscTitle.edition,
            db_models.DiscTitle.type,
            db_models.DiscTitle.season,
            db_models.DiscTitle.episode,
            db_models.DiscTitle.content,
            db_models.DiscTitle.cover_url,
            db_models.DiscTitle.language_code,
            db_models.DiscTitle.language,
            db_models.DiscTitle.detection_confidence,
            db_models.DiscTitle.detection_warning,
            db_models.DiscTitle.file_path,
            db_models.DiscTitle.file_path_stage,
        ),
        selectinload(db_models.Disc.title_streams),
        # _disc_record reads `disc.release.slug` for the release_slug
        # response field — eager-load to avoid an additional lazy SELECT.
        joinedload(db_models.Disc.release).load_only(
            db_models.Release.id,
            db_models.Release.slug,
        ),
    )


@router.get("/disc/{disc_id}", response_model=DiscRecord)
def get_disc_record(disc_id: str, content_hash: str | None = None, db: Session = Depends(get_db)):
    # Allow callers that matched the param route with disc_id="by-hash".
    if disc_id == "by-hash":
        if not content_hash:
            raise HTTPException(400, detail="content_hash is required")
        disc = (
            db.query(db_models.Disc)  # type: ignore[attr-defined]
            .options(*_disc_record_load_options())
            .filter(db_models.Disc.content_hash == content_hash)  # type: ignore
            .first()
        )
        if not disc:
            disc = crud.get_or_create_disc(db, content_hash, None, {})
        return _disc_record(disc, db)
    disc = (
        db.query(db_models.Disc)  # type: ignore[attr-defined]
        .options(*_disc_record_load_options())
        .filter(db_models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    return _disc_record(disc, db)


@router.patch("/disc/{disc_id}", response_model=DiscRecord)
def patch_disc_record(disc_id: str, req: DiscMetadataPatch, db: Session = Depends(get_db)):
    disc = (
        db.query(db_models.Disc)  # type: ignore[attr-defined]
        .filter(db_models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    _ensure_not_finalized(disc, "Disc")
    updates: Dict[str, Any] = {}
    if req.release_id:
        updates["release_id"] = req.release_id
    # Don't look up release by disc_group (slug) - this can cause wrong assignments
    # If release_id is not provided, we can't safely determine which release to use
    # The caller should provide release_id or use update_disc_metadata with movie_id
    if req.disc_group and "release_id" not in updates:
        # Skip slug-based lookup - it's not safe for boxsets
        # Caller should provide release_id directly or use movie_id-based lookup
        pass
    if req.disc_number is not None:
        updates["disc_number"] = req.disc_number
    if req.disc_slug is not None:
        updates["disc_slug"] = req.disc_slug
    if req.disc_name is not None:
        updates["disc_name"] = req.disc_name
    if req.disc_format is not None:
        updates["format"] = req.disc_format
    # DiscDB dirty-detection (#741) — but only for user-CORRECTION surfaces
    # whose value actually changed. Re-linking a release, renumbering or
    # re-slugging a disc is local organization, not "TheDiscDB has this
    # wrong", and a no-op save is not an edit at all.
    corrects_user_surface = (
        req.disc_name is not None and (req.disc_name or None) != (disc.disc_name or None)
    ) or (
        req.disc_format is not None and (req.disc_format or None) != (disc.format or None)
    )
    if updates:
        # Human edit path; pipeline writes never come through here.
        from datetime import datetime, timezone

        if corrects_user_surface:
            updates["user_edited_at"] = datetime.now(timezone.utc)
        db.query(db_models.Disc).filter(db_models.Disc.id == disc.id).update(updates)
        db.commit()
        db.refresh(disc)
    return _disc_record(disc, db)


@router.get("/disc/{disc_id}/tracks", response_model=List[DiscTitleRecord])
def list_disc_title_records(disc_id: str, db: Session = Depends(get_db)):
    disc = (
        db.query(db_models.Disc)  # type: ignore[attr-defined]
        .options(joinedload(db_models.Disc.titles))
        .filter(db_models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    return _disc_record(disc, db).titles


@router.patch("/disc/{disc_id}/tracks", response_model=List[DiscTitleRecord])
def patch_disc_title_records(disc_id: str, tracks: List[TitleLabel], db: Session = Depends(get_db)):
    disc = (
        db.query(db_models.Disc)  # type: ignore[attr-defined]
        .filter(db_models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    _ensure_not_finalized(disc, "Disc")
    logger.info("patch_disc_title_records: disc_id=%s count=%d", disc_id, len(tracks))

    new_titles = []
    for idx, t in enumerate(tracks):
        normalized_type = _normalize_title_type(t.type)
        # source_file is required - use track_id or title_id as fallback only (never idx)
        source = t.source_file or t.track_id or t.title_id
        if not source:
            import logging
            logger = logging.getLogger(__name__)
            logger.warning(f"Missing source_file for track at index {idx}: {getattr(t, 'title', 'unknown')}")
            continue  # Skip this track if source_file is missing
        
        # Use order_index from payload if provided (frontend sends sorted order),
        # otherwise fall back to array index
        order_idx = getattr(t, 'order_index', None)
        if order_idx is None:
            order_idx = idx
        else:
            try:
                order_idx = int(order_idx)
            except (ValueError, TypeError):
                order_idx = idx
        
        new_titles.append(
            db_models.DiscTitle(
                id=str(uuid.uuid4()),
                disc_id=disc.id,
                source_file=str(source),
                # User-submitted labels: mirror into user_* at construction
                # (resolved cache = user value; automation can't overwrite).
                # Without the mirror, a later auto write would resolve over
                # the raw resolved-only value (user None ?? auto).
                title=t.title,
                user_title=t.title,
                description=t.description,
                user_description=t.description,
                order_index=order_idx,
                season=t.season,
                user_season=t.season,
                episode=t.episode,
                user_episode=t.episode,
                type=normalized_type,
                user_type=normalized_type,
                comment=t.comment or t.note,
                duration=t.duration,
                size=t.size,
                streams=t.streams,
                content=True,
            )
        )
    disc.titles = new_titles
    db.commit()
    db.refresh(disc)
    return _disc_record(disc, db).titles


@router.get("/{slug}/export")
def export_release(slug: str, format: str = "json", db: Session = Depends(get_db)):
    """
    Build a best-effort export payload for a release using finalized artifacts.
    Reads finalize_result paths for each disc (release.json + discNN.json) and returns combined JSON.
    If format=zip, returns a zip of the finalized release folder (metadata dir if available).
    """
    rel = (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .filter(crud.models.Release.slug == slug)  # type: ignore
        .first()
    )
    if not rel:
        raise HTTPException(404, detail="Release not found")

    # Load release.json from first finalized disc if present
    release_json: Dict[str, Any] | None = None
    discs_payload: list[Dict[str, Any]] = []
    metadata_dir: Path | None = None
    for disc in rel.discs or []:
        fin = disc.finalize_result or {}
        disc_json_path = fin.get("disc_json") if isinstance(fin, dict) else None
        rel_json_path = fin.get("release_json") if isinstance(fin, dict) else None
        disc_json = None
        if disc_json_path and Path(disc_json_path).exists():
            try:
                disc_json = discdb_finalize._load_json(Path(disc_json_path))  # type: ignore[attr-defined]
            except Exception:
                disc_json = None
        if rel_json_path and Path(rel_json_path).exists() and release_json is None:
            try:
                release_json = discdb_finalize._load_json(Path(rel_json_path))  # type: ignore[attr-defined]
            except Exception:
                release_json = None
        if isinstance(fin, dict):
            meta_path = fin.get("metadata_dir")
            if meta_path and Path(meta_path).exists():
                metadata_dir = Path(meta_path)
        discs_payload.append(
            {
                "content_hash": disc.content_hash,
                "disc_slug": disc.disc_slug,
                "disc_number": disc.disc_number,
                "finalize_result": fin or None,
                "disc_json": disc_json,
            }
        )

    # If no finalize artifacts were found, treat as error
    if release_json is None:
        raise HTTPException(400, detail="No finalized release.json found; finalize the release before export")

    if format.lower() == "zip":
        base_dir = metadata_dir
        if not base_dir and discs_payload:
            # fallback to release_json dir if present in first disc finalize_result
            first_fin = (rel.discs or [None])[0].finalize_result if rel.discs else None  # type: ignore[attr-defined]
            if isinstance(first_fin, dict):
                rel_dir = first_fin.get("release_dir")
                if rel_dir and Path(rel_dir).exists():
                    base_dir = Path(rel_dir)
        if not base_dir or not base_dir.exists():
            raise HTTPException(404, detail="No finalized artifacts to export")
        
        # Ensure ZIP structure matches DiscDB layout: movie/ or series/ at root
        # Find the parent directory that contains movie/ or series/
        export_root = get_export_root()
        rel_type = (rel.type or "movie").strip().lower() or "movie"
        
        # Find the movie/ or series/ directory level
        current = Path(base_dir)
        type_dir = None
        while current != export_root.parent and current != Path("/"):
            if current.name == rel_type and current.parent == export_root:
                type_dir = current
                break
            current = current.parent
        
        # If we found the type directory, use it as root; otherwise use base_dir
        zip_root = type_dir if type_dir else base_dir
        
        tmp_dir = get_mkvauto_root() / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        zip_path = tmp_dir / f"{rel.slug}-export.zip"
        try:
            if zip_path.exists():
                zip_path.unlink()
            shutil.make_archive(str(zip_path.with_suffix('')), 'zip', root_dir=zip_root)
        except Exception as exc:
            raise HTTPException(500, detail=f"Failed to build export zip: {exc}") from exc
        return FileResponse(zip_path, filename=zip_path.name, media_type="application/zip")

    return JSONResponse(
        {
            "release": release_json,
            "discs": discs_payload,
        }
    )

@router.patch("/{id_or_slug}", response_model=ReleaseSummary)
def update_release(id_or_slug: str, payload: ReleaseMetadataPatch, db: Session = Depends(get_db)):
    """
    Update release metadata directly (slug/name/type/ids/covers/years) by id or slug.
    """
    rel = _get_release_by_id(db, id_or_slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")

    from core.release_link_validation import normalize_gtin_from_discdb

    missing_before = crud.release_missing_required_field_keys(db, rel)
    data = payload.model_dump(exclude_none=True)

    # Handle slug changes.
    new_slug = data.get("release_slug") or data.get("disc_group")
    if new_slug and new_slug != rel.slug:
        rel.slug = new_slug

    new_type = data.get("group_type") or data.get("mode")
    if new_type:
        rel.type = "series" if new_type == "series" else "movie"

    if "release_name" in data:
        rn = data["release_name"]
        # #633: reject empty ``release_name`` outright. A release must have a
        # name — that's the load-bearing invariant Library rendering,
        # notifications, and destination path templating depend on. There is
        # no legitimate reason a client would PATCH a release with an empty
        # name; the previous "silently null it out" behaviour let the
        # frontend prefill bug (DiscDB-auto-created stubs seeding empty
        # ``model.name``, then Save & link submitting it back) hide as
        # "(untitled)" releases in the Library. Fail loudly at the API so
        # future frontend regressions surface immediately.
        if rn is None or not str(rn).strip():
            raise HTTPException(
                400,
                detail={
                    "error": "release_name must be a non-empty string",
                    "missing": ["release_name"],
                },
            )
        rel.name = str(rn).strip()
    # tmdb_id removed from Release - use Movie.tmdb_id instead
    if data.get("upc") is not None:
        rel.upc = normalize_gtin_from_discdb(data.get("upc"))
    if data.get("asin") is not None:
        rel.asin = data.get("asin")
    if data.get("cover_front_url") is not None:
        rel.cover_front_url = data.get("cover_front_url")
    if data.get("cover_back_url") is not None:
        rel.cover_back_url = data.get("cover_back_url")
    prior_year = getattr(rel, "release_year", None)
    if data.get("release_year") is not None:
        rel.release_year = data.get("release_year")
    if data.get("original_year") is not None:
        rel.original_year = data.get("original_year")

    # Auto-build slug when year is supplied, or when we previously had a pending slug.
    desired_slug = _compute_release_slug(rel, data)
    if desired_slug and desired_slug != rel.slug:
        if not rel.slug or rel.slug.startswith("pending-") or (prior_year != rel.release_year):
            conflict = (
                db.query(crud.models.Release)  # type: ignore[attr-defined]
                .filter(crud.models.Release.slug == desired_slug, crud.models.Release.id != rel.id)  # type: ignore
                .first()
            )
            if conflict is None:
                rel.slug = desired_slug

    db.flush()
    db.refresh(rel)
    missing_after = crud.release_missing_required_field_keys(db, rel)
    if missing_before and not missing_after:
        rel.modified = True

    db.commit()
    db.refresh(rel)
    return _release_summary(rel, db)


@router.patch("/disc/{disc_id}/metadata")
def update_disc_metadata(disc_id: str, payload: DiscMetadataUpdate, db: Session = Depends(get_db)):
    """
    Dedicated autosave endpoint to persist release/disc/track metadata.
    - Updates the linked release (or finds/creates release by movie_id and boxset_id).
    - Creates a new release only when required fields (movie_id) are present.
    - Persists title metadata and rebuilds title_streams rows.
    - Uses movie_id and boxset_id for release lookup, NOT slug.
    """
    disc = (
        db.query(crud.models.Disc)  # type: ignore[attr-defined]
        .filter(crud.models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    _ensure_not_finalized(disc, "Disc")

    release_data = payload.release.model_dump(exclude_none=True) if payload.release else {}
    disc_data = payload.disc.model_dump(exclude_none=True) if payload.disc else {}
    # Accept both payload.titles (schema) and payload.tracks (legacy callers/tests).
    tracks_payload = payload.titles or []
    legacy_tracks = getattr(payload, "tracks", None)
    if legacy_tracks:
        tracks_payload = tracks_payload or legacy_tracks
    tracks = [t.model_dump(exclude_none=True) for t in tracks_payload] if tracks_payload else []

    target_release = disc.release
    target_slug = release_data.get("release_slug") or disc_data.get("disc_group")
    target_id = release_data.get("release_id")

    # Prefer linking by release_id when provided.
    if target_id:
        existing_by_id = (
            db.query(crud.models.Release)  # type: ignore[attr-defined]
            .filter(crud.models.Release.id == target_id)  # type: ignore
            .first()
        )
        if existing_by_id:
            target_release = existing_by_id
            # Once a release is linked, its movie_id should NOT be updated from label_draft.
            # The release's movie_id is authoritative and should only be changed explicitly by the user.

    # Only find-or-create a release when client explicitly asked to link one (release_id provided).
    # Never auto-create a release from movie_id when release_id is missing; use Create New Release or Create Boxset endpoints.
    need_release = target_release is None or (target_id and target_release and target_release.id != target_id)
    if need_release and not target_id:
        # Client did not send release_id; do not create or assign a release
        need_release = False
    if need_release and target_id and target_release is None:
        # Client sent release_id but it was not found
        raise HTTPException(404, detail="Release not found")
    
    # Get movie_id and boxset_id from payload (for metadata updates and for find-existing-only below)
    movie_id = release_data.get("movie_id") or release_data.get("film_id")
    if not movie_id:
        label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
        movie_id = label_draft.get("movie_id") or label_draft.get("film_id")
    boxset_id = release_data.get("boxset_id")
    
    if need_release and target_id and target_release:
        # target_release was set from existing_by_id (same as target_id); nothing more to do for linking
        pass
    elif need_release and target_id:
        # target_release is None but target_id was provided - already raised 404 above
        pass

    if target_release:
        # Optionally find existing release by movie_id when current release's movie_id does not match payload (assign only, never create)
        if movie_id:
            release_movie_id = target_release.movie_id
            if release_movie_id != movie_id:
                log.warning(
                    "Release %s has movie_id %s but payload has movie_id %s - looking for existing release only (no auto-create)",
                    target_release.id, release_movie_id, movie_id,
                )
                if boxset_id:
                    correct_release = (
                        db.query(crud.models.Release)
                        .filter(
                            crud.models.Release.movie_id == movie_id,
                            crud.models.Release.boxset_id == boxset_id
                        )
                        .first()
                    )
                else:
                    correct_release = (
                        db.query(crud.models.Release)
                        .filter(
                            crud.models.Release.movie_id == movie_id,
                            crud.models.Release.boxset_id.is_(None)
                        )
                        .first()
                    )
                if correct_release:
                    target_release = correct_release
                # If not found, leave target_release as-is; do not create
        if release_data.get("group_type"):
            target_release.type = release_data["group_type"]
        if release_data.get("mode") and not release_data.get("group_type"):
            target_release.type = "series" if release_data["mode"] == "series" else "movie"
        _ensure_not_finalized(target_release, "Release")
        if release_data.get("release_name"):
            target_release.name = release_data["release_name"]
            target_release.name = target_release.name or release_data["release_name"]
        # tmdb_id removed from Release - use Movie.tmdb_id instead
        if release_data.get("upc"):
            target_release.upc = release_data["upc"]
        if release_data.get("asin"):
            target_release.asin = release_data["asin"]
        if release_data.get("cover_front_url"):
            target_release.cover_front_url = release_data["cover_front_url"]
        if release_data.get("cover_back_url"):
            target_release.cover_back_url = release_data["cover_back_url"]
        if release_data.get("release_year") is not None:
            target_release.release_year = release_data["release_year"]

    # Link release to boxset before link-readiness check when payload includes boxset_id.
    # Frontend omits standalone UPC when boxset is selected; add_release_to_boxset copies
    # boxset UPC/cover so release_link_ready uses boxset rules, not standalone missing-upc.
    boxset_changed = False
    if boxset_id and target_release:
        old_boxset_id = target_release.boxset_id
        if target_release.boxset_id != boxset_id:
            boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_id).first()
            if boxset:
                crud.add_release_to_boxset(db, boxset, target_release)
                boxset_changed = old_boxset_id != boxset_id
        db.refresh(target_release)

    # Track if release changed to recalculate disc number
    release_changed = target_release and disc.release_id != target_release.id
    if release_changed and target_release:
        if not crud.release_link_ready(db, target_release):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "release_not_link_ready",
                    "missing": crud.release_missing_required_field_keys(db, target_release),
                },
            )
    if release_changed:
        # Get old release before changing
        old_release_id = disc.release_id
        disc.release_id = target_release.id
        # Cleanup orphaned release if it has no other discs
        if old_release_id:
            old_release = db.query(db_models.Release).filter(db_models.Release.id == old_release_id).first()
            if old_release:
                from api.crud import cleanup_orphaned_release
                cleanup_orphaned_release(db, old_release)

    # Normalize disc numbers if requested (e.g., when continuing from boxset/release step)
    # or when release/boxset changes (to ensure uniqueness)
    recalculate_disc_numbers = release_data.get("recalculate_disc_numbers", False)
    should_normalize = recalculate_disc_numbers or (release_changed or boxset_changed)
    if should_normalize and target_release:
        # Ensure the disc release_id/boxset changes are visible before normalization.
        db.flush()
        from api.crud import normalize_disc_numbers_for_release
        disc_number_map = normalize_disc_numbers_for_release(db, target_release)
        disc.disc_number = disc_number_map.get(disc.id, disc.disc_number)
        # Refresh to get updated disc numbers
        db.refresh(target_release)
        db.refresh(disc)
    elif target_release and (disc.disc_number is None or release_changed or boxset_changed or target_release.boxset_id):
        # Calculate next disc number using release-only or boxset-wide logic.
        # Use all discs (finished or not) to keep numbering consistent across releases/boxsets.
        from api.crud import _next_disc_numbers_all
        disc.disc_number = _next_disc_numbers_all(db, target_release, exclude_disc_id=disc.id)

    # User explicitly provided disc_number - use it (but only if not normalizing)
    if disc_data.get("disc_number") is not None and not should_normalize:
        disc.disc_number = disc_data["disc_number"]
    if disc_data.get("disc_name"):
        disc.disc_name = disc_data["disc_name"]
    if disc_data.get("disc_format"):
        disc.format = disc_data["disc_format"]
    if "disc_slug" in disc_data or "disc_name" in disc_data:
        crud.apply_disc_slug_from_label_payload(disc, disc_data.get("disc_slug"))

    if tracks:
        # Replace existing titles/streams with the new set (autosave semantics).
        try:
            disc.title_streams.clear()
        except Exception:
            pass
        try:
            disc.titles.clear()
        except Exception:
            pass
        payload_by_source: Dict[str, Dict[str, Any]] = {}
        new_titles = []
        seen: set[str] = set()
        for idx, t in enumerate(tracks):
            # source_file is required - use track_id or title_id as fallback only (never idx)
            source = t.get("source_file") or t.get("track_id") or t.get("title_id")
            if not source:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Missing source_file for track at index {idx}: {t.get('title', 'unknown')}")
                # Skip this track if source_file is missing - don't use idx as fallback
                continue
            source_key = str(source)
            if source_key in seen:
                continue
            seen.add(source_key)
            payload_by_source[source_key] = t
            # Use order_index from payload if provided (frontend sends sorted order),
            # otherwise fall back to array index
            order_idx = t.get("order_index")
            if order_idx is None:
                order_idx = idx
            else:
                try:
                    order_idx = int(order_idx)
                except (ValueError, TypeError):
                    order_idx = idx
            
            new_titles.append(
                db_models.DiscTitle(
                    id=str(uuid.uuid4()),
                    disc_id=disc.id,
                    index=idx,
                    order_index=order_idx,
                    comment=t.get("comment"),
                    source_file=source_key,
                    segment_map=t.get("segment_map"),
                    duration=t.get("duration"),
                    duration_raw=t.get("duration_raw"),
                    size=t.get("size"),
                    display_size=t.get("display_size"),
                    # User-submitted labels: mirror into user_* (see
                    # patch_disc_title_records above for why).
                    description=t.get("description") or t.get("note"),
                    user_description=t.get("description") or t.get("note"),
                    title=t.get("title") or t.get("episode_name"),
                    user_title=t.get("title") or t.get("episode_name"),
                    type=_normalize_title_type(t.get("type")),
                    user_type=_normalize_title_type(t.get("type")),
                    season=t.get("season"),
                    user_season=t.get("season"),
                    episode=t.get("episode"),
                    user_episode=t.get("episode"),
                    chapters=t.get("chapters"),
                    streams=t.get("streams"),
                    language_code=t.get("language_code"),
                    language=t.get("language"),
                    content=t.get("content", True),
                )
            )
        disc.titles = new_titles

        new_stream_rows = []
        for title in new_titles:
            payload = payload_by_source.get(title.source_file) or {}
            streams = payload.get("streams") or []
            if not streams:
                new_stream_rows.append(
                    db_models.TitleStream(
                        id=str(uuid.uuid4()),
                        disc_id=disc.id,
                        title_id=title.id,
                        stream_index=0,
                        stream_type=None,
                        audio_type=None,
                        language_code=None,
                        language=None,
                        codec_short=None,
                        codec_hint=None,
                        name=None,
                        bitrate=None,
                        channels=None,
                        sample_rate=None,
                        bit_depth=None,
                        resolution=None,
                        aspect_ratio=None,
                        reference_frames=None,
                        description=None,
                        info=None,
                        duration_seconds=None,
                        flag=None,
                        default=None,
                        layout=None,
                        frame_rate=None,
                        title=title.title,
                        note=title.comment or title.description,
                        duration=title.duration,
                        size=title.size,
                        streams=None,
                        content=title.content,
                        order_index=0,
                    )
                )
                continue
            seen_streams: set[int] = set()
            for s_idx, stream in enumerate(streams):
                if s_idx in seen_streams:
                    continue
                seen_streams.add(s_idx)
                new_stream_rows.append(
                    db_models.TitleStream(
                        id=str(uuid.uuid4()),
                        disc_id=disc.id,
                        title_id=title.id,
                        stream_index=s_idx,
                        stream_type=stream.get("type"),
                        audio_type=stream.get("audio_type"),
                        language_code=stream.get("language_code"),
                        language=stream.get("language"),
                        codec_short=stream.get("codec_short"),
                        codec_hint=stream.get("codec_hint"),
                        name=stream.get("name"),
                        bitrate=stream.get("bitrate"),
                        channels=stream.get("channels") if isinstance(stream.get("channels"), (int, type(None))) else None,
                        sample_rate=stream.get("sample_rate"),
                        bit_depth=stream.get("bit_depth"),
                        resolution=stream.get("resolution"),
                        aspect_ratio=stream.get("aspect_ratio"),
                        frame_rate=stream.get("frame_rate"),
                        reference_frames=stream.get("reference_frames"),
                        description=stream.get("description"),
                        info=stream.get("info"),
                        duration_seconds=stream.get("duration_seconds"),
                        flag=stream.get("flag"),
                        default=stream.get("default"),
                        layout=stream.get("layout"),
                        title=title.title,
                        note=stream.get("note"),
                        duration=title.duration,
                        size=title.size,
                        streams=stream,
                        content=title.content,
                        order_index=s_idx,
                    )
                )
        db.add_all(new_stream_rows)
        disc.title_streams = new_stream_rows
        db.flush()
        db.refresh(disc)

    if target_release and disc.release_id == target_release.id and crud.release_link_ready(db, target_release):
        crud.sync_disc_pending_release_metadata(db, disc, target_release, True)
        crud.sync_disc_label_draft_with_release(disc, target_release)

    from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

    sync_duplicate_group_labels_for_disc(db, str(disc.id))
    db.commit()
    db.refresh(disc)
    if target_release:
        db.refresh(target_release)

    latest_job = sorted(disc.jobs, key=lambda j: j.created_at)[-1] if disc.jobs else None
    return {
        "disc": DiscSummary(
            id=disc.id,
            content_hash=disc.content_hash,
            release_id=target_release.id if target_release else None,
            release_slug=target_release.slug if target_release else None,
            disc_number=disc.disc_number,
            discdb_disc_num=getattr(disc, "discdb_disc_num", None),
            disc_slug=disc.disc_slug,
            disc_name=disc.disc_name,
            format=disc.format,
            label_present=bool(disc.title_streams),
            finalized=bool(getattr(disc, "finalized", False) or disc.finalize_result),
            latest_job_id=str(latest_job.id) if latest_job else None,
            latest_job_status=latest_job.job_status if latest_job else None,
            latest_job_progress=latest_job.rip_progress if latest_job else None,
            transfer_state=getattr(latest_job, "transfer_state", None) if latest_job else None,
            discdb_hit=bool(disc.content_hash),
        ),
        "release": _release_summary(target_release, db) if target_release and db else (
            ReleaseSummary(
                id=target_release.id,
                slug=target_release.slug,
                type=target_release.type,
                name=target_release.name,
                title=target_release.name,
                movie_id=getattr(target_release, "movie_id", None),
                movie=MovieSummary(
                    id=target_release.movie.id,
                    name=target_release.movie.name,
                    production_year=target_release.movie.production_year,
                    tmdb_id=target_release.movie.tmdb_id,
                    tmdb_type=target_release.movie.tmdb_type,
                    cover_url=target_release.movie.cover_url,
                    cover_path=target_release.movie.cover_path,
                ) if hasattr(target_release, "movie") and target_release.movie else None,
                tmdb_id=target_release.movie.tmdb_id if target_release.movie else None,
                upc=target_release.upc,
                asin=target_release.asin,
                cover_front_url=target_release.cover_front_url,
                cover_back_url=target_release.cover_back_url,
                finalize_state=target_release.finalize_state,
                total_discs=len(target_release.discs) if target_release.discs else 0,
                completed_discs=0,
                finalized_discs=0,
                release_year=release_data.get("release_year") or getattr(target_release, "release_year", None),
                original_year=release_data.get("production_year") or release_data.get("original_year") or getattr(target_release, "production_year", None),
                production_year=release_data.get("production_year") or release_data.get("original_year") or getattr(target_release, "production_year", None),
                resolution=getattr(target_release, "resolution", None),
                discdb_hit=True,
            ) if target_release else None
        ),
    }


def _safe_remove_path(p: str | Path | None):
    if not p:
        return
    try:
        path = Path(p)
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink(missing_ok=True)
    except Exception:
        pass


def _cleanup_job_files(job):
    job_paths = JobPaths.for_id(str(job.id))
    if job_paths.root.exists():
        job_dir_path = job_paths.root
        # Remove previews directory
        try:
            _safe_remove_path(job_dir_path / "previews")
        except Exception:
            pass
        # Remove files from ripped_files (in raw/)
        ripped_files = getattr(job, "ripped_files", None)
        if isinstance(ripped_files, dict):
            raw_dir = job_dir_path / "raw"
            for rel_path in ripped_files.values():
                try:
                    _safe_remove_path(raw_dir / rel_path)
                except Exception:
                    pass
        # Remove files from post_paths (in transient/)
        post_paths = getattr(job, "post_paths", None)
        if isinstance(post_paths, dict):
            transient_dir = job_dir_path / "transient"
            for rel_path in post_paths.values():
                try:
                    _safe_remove_path(transient_dir / rel_path)
                except Exception:
                    pass


@router.post("/disc/{disc_id}/label")
def save_disc_label(disc_id: str, label: LabelRequest, db: Session = Depends(get_db)):
    """
    Save label metadata directly to a disc (no active job required).
    """
    disc = (
        db.query(crud.models.Disc)  # type: ignore[attr-defined]
        .filter(crud.models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    _ensure_not_finalized(disc, "Disc")

    lp: Dict[str, Any] = label.model_dump()
    disc.disc_name = lp.get("disc_name") or disc.disc_name
    disc.format = lp.get("disc_format") or disc.format
    # model_fields_set: only derive slug when disc_slug/disc_name were in the request body
    fs = label.model_fields_set
    slug_in = "disc_slug" in fs
    name_in = "disc_name" in fs
    if slug_in:
        raw = label.disc_slug
        if raw is not None and str(raw).strip() != "":
            disc.disc_slug = str(raw).strip()
        elif name_in:
            crud.apply_disc_slug_from_label_payload(disc, None)
    elif name_in:
        crud.apply_disc_slug_from_label_payload(disc, None)
    if lp.get("disc_number") is not None:
        if lp.get("disc_number") is not None:
            disc.disc_number = lp.get("disc_number")
    elif disc.disc_number is None and disc.release:
        # Calculate next disc number using release-only or boxset-wide logic.
        # Use all discs to keep numbering consistent across releases/boxsets.
        from api.crud import _next_disc_numbers_all
        disc.disc_number = _next_disc_numbers_all(db, disc.release, exclude_disc_id=disc.id)

    release = disc.release
    if not release:
        # Use movie_id and boxset_id for lookup, NOT slug
        name = lp.get("release_name") or None
        movie_id = lp.get("movie_id") or lp.get("film_id")
        boxset_id = lp.get("boxset_id")
        
        if movie_id:
            # Look up existing release by movie_id and boxset_id (NOT by slug)
            if boxset_id:
                existing = (
                    db.query(crud.models.Release)
                    .filter(
                        crud.models.Release.movie_id == movie_id,
                        crud.models.Release.boxset_id == boxset_id
                    )
                    .first()
                )
            else:
                existing = (
                    db.query(crud.models.Release)
                    .filter(
                        crud.models.Release.movie_id == movie_id,
                        crud.models.Release.boxset_id.is_(None)
                    )
                    .first()
                )
            
            if existing:
                release = existing
                disc.release_id = release.id
            elif name:
                # No release exists - create new one
                # Verify movie exists
                movie = db.query(db_models.Movie).filter(db_models.Movie.id == movie_id).first()
                if movie:
                    # Auto-generate slug from name and year
                    # Skip slug generation if boxset is linked (will use boxset.slug)
                    year = lp.get("release_year") or None
                    new_slug = "pending"
                    if not boxset_id:
                        desired_slug = _compute_release_slug(None, {
                            "release_year": year,
                            "release_name": name,
                        })
                        if desired_slug:
                            new_slug = desired_slug
                            # ensure uniqueness by suffixing if needed
                            base_slug = new_slug
                            idx = 1
                            while db.query(crud.models.Release).filter(crud.models.Release.slug == new_slug).first():  # type: ignore[attr-defined]
                                new_slug = f"{base_slug}-{idx}"
                                idx += 1
                    
                    # If boxset is provided, use boxset.slug instead
                    final_slug = new_slug
                    if boxset_id:
                        boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_id).first()  # type: ignore
                        if boxset and boxset.slug:
                            final_slug = boxset.slug
                    
                    release = crud.models.Release(  # type: ignore[attr-defined]
                        slug=final_slug,
                        type=lp.get("group_type") or lp.get("mode") or "movie",
                        name=name or final_slug,
                        title=name or final_slug,
                        movie_id=movie_id,
                        upc=lp.get("upc"),
                        asin=lp.get("asin"),
                        cover_front_url=lp.get("cover_front_url"),
                        cover_back_url=lp.get("cover_back_url"),
                        release_year=lp.get("release_year"),
                        boxset_id=boxset_id,
                    )
                    db.add(release)
                    db.flush()
                    disc.release_id = release.id
    else:
        _ensure_not_finalized(release, "Release")
        release.type = lp.get("group_type") or release.type
        release.name = lp.get("release_name") or release.name
        # Release slug is auto-generated, ignore if provided
        # release.slug = lp.get("release_slug") or release.slug
        # tmdb_id removed from Release - use Movie.tmdb_id instead
        release.upc = lp.get("upc") or release.upc
        release.asin = lp.get("asin") or release.asin
        release.cover_front_url = lp.get("cover_front_url") or release.cover_front_url
        release.cover_back_url = lp.get("cover_back_url") or release.cover_back_url
        if lp.get("release_year") is not None:
            release.release_year = lp.get("release_year")
        
        # Auto-generate slug when release year or name is updated
        # BUT skip if release is part of boxset (boxset.slug is authoritative)
        if lp.get("release_year") is not None or lp.get("release_name") is not None:
            if not release.boxset_id:
                desired_slug = _compute_release_slug(release, {
                    "release_year": lp.get("release_year") or getattr(release, "release_year", None),
                    "release_name": lp.get("release_name") or getattr(release, "name", None),
                })
                if desired_slug:
                    release.slug = desired_slug

    # Persist titles to disc_titles (authoritative for UI metadata).
    if lp.get("titles"):
        logger.info("save_disc_label titles: disc_id=%s count=%d", disc.id, len(lp["titles"]))
        existing = {
            str(getattr(t, "id", "")): t
            for t in getattr(disc, "titles", []) or []
            if getattr(t, "id", None)
        }
        seen: set[str] = set()
        new_titles = []
        for idx, t in enumerate(lp["titles"]):
            title_id = t.get("title_id")
            if not title_id:
                logger.warning("Missing title_id for title at index %s: %s", idx, t.get("title", "unknown"))
                continue
            title_key = str(title_id)
            if title_key in seen:
                continue  # de-duplicate payload entries
            seen.add(title_key)
            target = existing.get(title_key)
            if not target:
                target = db_models.DiscTitle(
                    id=title_key,
                    disc_id=disc.id,
                    source_file=t.get("source_file"),
                )
            if t.get("source_file"):
                target.source_file = t.get("source_file")
            # Release labelForm save is user-initiated; route every label
            # field through the source-aware helper so user_* owns the
            # values (and automation can never overwrite them later).
            from api.crud import set_title_field
            # If title is explicitly in payload (even if None/empty), use it; otherwise fall back to episode_name
            if "title" in t:
                set_title_field(target, "title", t.get("title"), source="user")
            else:
                set_title_field(target, "title", t.get("episode_name"), source="user")
            set_title_field(target, "description", t.get("description") or t.get("note"), source="user")
            set_title_field(target, "season", t.get("season"), source="user")
            set_title_field(target, "episode", t.get("episode"), source="user")
            set_title_field(target, "type", _normalize_title_type(t.get("type")), source="user")
            target.comment = t.get("comment") or t.get("note")
            target.duration = t.get("duration")
            target.size = t.get("size")
            target.streams = t.get("streams")
            target.content = True
            target.order_index = idx
            new_titles.append(target)
        disc.titles = new_titles

        try:
            disc.title_streams.clear()
        except Exception:
            pass
        new_stream_rows = []
        for title in new_titles:
            payload = next((p for p in lp["titles"] if str(p.get("source_file") or p.get("track_id") or p.get("title_id") or "") == str(title.source_file)), None) or {}
            streams = payload.get("streams") or []
            if not streams:
                new_stream_rows.append(
                    db_models.TitleStream(
                        id=str(uuid.uuid4()),
                        disc_id=disc.id,
                        title_id=title.id,
                        stream_index=0,
                        stream_type=None,
                        audio_type=None,
                        language_code=None,
                        language=None,
                        codec_short=None,
                        codec_hint=None,
                        name=None,
                        bitrate=None,
                        channels=None,
                        sample_rate=None,
                        bit_depth=None,
                        resolution=None,
                        aspect_ratio=None,
                        reference_frames=None,
                        description=None,
                        info=None,
                        duration_seconds=None,
                        flag=None,
                        default=None,
                        layout=None,
                        frame_rate=None,
                        title=title.title,
                        note=title.comment or title.description,
                        duration=title.duration,
                        size=title.size,
                        streams=None,
                        content=title.content,
                        order_index=0,
                    )
                )
                continue
            for s_idx, stream in enumerate(streams):
                new_stream_rows.append(
                    db_models.TitleStream(
                        id=str(uuid.uuid4()),
                        disc_id=disc.id,
                        title_id=title.id,
                        stream_index=s_idx,
                        stream_type=stream.get("type"),
                        audio_type=stream.get("audio_type"),
                        language_code=stream.get("language_code"),
                        language=stream.get("language"),
                        codec_short=stream.get("codec_short"),
                        codec_hint=stream.get("codec_hint"),
                        name=stream.get("name"),
                        bitrate=stream.get("bitrate"),
                        channels=stream.get("channels") if isinstance(stream.get("channels"), (int, type(None))) else None,
                        sample_rate=stream.get("sample_rate"),
                        bit_depth=stream.get("bit_depth"),
                        resolution=stream.get("resolution"),
                        aspect_ratio=stream.get("aspect_ratio"),
                        frame_rate=stream.get("frame_rate"),
                        reference_frames=stream.get("reference_frames"),
                        description=stream.get("description"),
                        info=stream.get("info"),
                        duration_seconds=stream.get("duration_seconds"),
                        flag=stream.get("flag"),
                        default=stream.get("default"),
                        layout=stream.get("layout"),
                        title=title.title,
                        note=stream.get("note"),
                        duration=title.duration,
                        size=title.size,
                        streams=stream,
                        content=title.content,
                        order_index=s_idx,
                    )
                )
        disc.title_streams = new_stream_rows

        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc

        sync_duplicate_group_labels_for_disc(db, str(disc.id))

    # Link release to boxset if boxset is specified
    if release:
        # Handle boxset linking via boxset_id (boxset is a relationship, not a type)
        boxset_id = lp.get("boxset_id")
        boxset = None
        if boxset_id:
            # Try to find boxset by id
            boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == boxset_id).first()
        if not boxset:
            # Fall back to slug lookup for backward compatibility
            boxset_slug = lp.get("boxset_slug")
            if boxset_slug:
                boxset = crud.get_boxset_by_slug(db, boxset_slug)
        if boxset:
            crud.add_release_to_boxset(db, boxset, release)

    db.commit()
    db.refresh(disc)
    if release:
        db.refresh(release)

    latest_job = sorted(disc.jobs, key=lambda j: j.created_at)[-1] if disc.jobs else None
    if latest_job:
        payload = latest_job.disc_payload or {}
        payload["label_ready"] = True
        payload["label_required"] = payload.get("label_required", True)
        latest_job.disc_payload = payload
        finalize_state = "ready" if (latest_job.stage_profile or "miss") == "miss" else "skipped"
        try:
            StageState.label_complete(
                db,
                latest_job,
                reason="disc metadata updated",
                finalize_state=finalize_state,
                job_status=latest_job.job_status or "running",
            )
        except StateViolation as exc:
            # Metadata updates are allowed even if job state is inconsistent; don't mask the issue.
            log.warning("Skipping job state update for %s due to state violation: %s", latest_job.id, exc)
    return {
        "disc": DiscSummary(
            id=disc.id,
            content_hash=disc.content_hash,
            release_id=release.id if release else None,
            release_slug=release.slug if release else None,
            disc_number=disc.disc_number,
            discdb_disc_num=getattr(disc, "discdb_disc_num", None),
            disc_slug=disc.disc_slug,
            disc_name=disc.disc_name,
            format=disc.format,
            label_present=bool(disc.titles),
            finalized=bool(getattr(disc, "finalized", False) or disc.finalize_result),
            latest_job_id=str(latest_job.id) if latest_job else None,
            latest_job_status=latest_job.job_status if latest_job else None,
            latest_job_progress=latest_job.rip_progress if latest_job else None,
            transfer_state=getattr(latest_job, "transfer_state", None) if latest_job else None,
        ),
        "release": ReleaseSummary(
            id=release.id if release else None,
            slug=release.slug if release else "",
            type=release.type if release else None,
            name=release.name if release else None,
            title=release.name if release else None,
            tmdb_id=release.movie.tmdb_id if release and release.movie else None,
            upc=release.upc if release else None,
            asin=release.asin if release else None,
            cover_front_url=release.cover_front_url if release else None,
            cover_back_url=release.cover_back_url if release else None,
            finalize_state=release.finalize_state if release else None,
            total_discs=len(release.discs) if release else 0,
            completed_discs=0,
            finalized_discs=0,
            release_year=getattr(release, "release_year", None) if release else None,
            production_year=getattr(release, "production_year", None) if release else None,
        ) if release else None,
    }


@router.delete("/disc/{disc_id}")
def delete_disc(disc_id: str, db: Session = Depends(get_db)):
    """
    Delete a disc, its jobs, and any associated transient files.
    """
    disc = (
        db.query(crud.models.Disc)  # type: ignore[attr-defined]
        .filter(crud.models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    jobs = list(disc.jobs or [])
    for job in jobs:
        _cleanup_job_files(job)
        db.delete(job)
    db.delete(disc)
    db.commit()
    return {"deleted_disc_id": disc_id, "deleted_jobs": len(jobs)}


@router.post("/disc/{disc_id}/finalize")
def finalize_disc(disc_id: str, db: Session = Depends(get_db)):
    """
    Finalize a disc independently of a job (requires label + artifacts).
    """
    disc = (
        db.query(crud.models.Disc)  # type: ignore[attr-defined]
        .filter(crud.models.Disc.id == disc_id)  # type: ignore
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")
    if getattr(disc, "finalized", False):
        return {"disc_id": disc.id, "finalize_result": disc.finalize_result}
    release = getattr(disc, "release", None)
    # Group title_streams by title_id to enrich titles below.
    stream_map: Dict[str, list[dict[str, Any]]] = {}
    for tr in getattr(disc, "title_streams", []) or []:
        key = getattr(tr, "title_id", None)
        if not key:
            continue
        streams = stream_map.setdefault(str(key), [])
        streams.append(
            {
                "index": getattr(tr, "stream_index", None),
                "type": getattr(tr, "stream_type", None),
                "audio_type": getattr(tr, "audio_type", None),
                "language_code": getattr(tr, "language_code", None),
                "language": getattr(tr, "language", None),
                "codec_short": getattr(tr, "codec_short", None),
                "codec_hint": getattr(tr, "codec_hint", None),
                "name": getattr(tr, "name", None),
                "resolution": getattr(tr, "resolution", None),
                "aspect_ratio": getattr(tr, "aspect_ratio", None),
            }
        )

    titles: list[dict[str, Any]] = []
    for t in getattr(disc, "titles", []) or []:
        track_id = getattr(t, "source_file", None) or getattr(t, "index", None)
        entry = {
            "track_id": track_id,
            "title_id": getattr(t, "id", None),
            "title": getattr(t, "title", None),
            "description": getattr(t, "description", None),
            "season": getattr(t, "season", None),
            "episode": getattr(t, "episode", None),
            "type": getattr(t, "type", None),
            "note": getattr(t, "description", None) or getattr(t, "comment", None),
            "comment": getattr(t, "comment", None),
            "duration": getattr(t, "duration", None) or getattr(t, "duration_seconds", None),
            "size": getattr(t, "size", None),
            "display_size": getattr(t, "display_size", None),
            "segment_map": getattr(t, "segment_map", None),
            "chapters": getattr(t, "chapters", None),
            "streams": stream_map.get(str(getattr(t, "id", None)), None),
            "content": getattr(t, "content", True),
            "index": getattr(t, "index", None),
            "source_file": getattr(t, "source_file", None),
        }
        titles.append(entry)
    if not titles:
        raise HTTPException(400, detail="No title records found; save labels before finalizing")

    label_payload: Dict[str, Any] = {
        "disc_slug": disc.disc_slug,
        "disc_name": disc.disc_name,
        "disc_number": disc.disc_number,
        "disc_format": disc.format,
        "titles": titles,
    }
    if release:
        # Extract movie/boxset/series name
        movie_name = None
        boxset_name = None
        if release.movie:
            movie_name = release.movie.name
        elif release.boxset and release.boxset.name:
            boxset_name = release.boxset.name
        
        label_payload.update(
            {
                "release_slug": release.slug,
                "release_name": release.name,
                "release_year": getattr(release, "release_year", None),
                "production_year": getattr(release, "production_year", None),
                "original_year": getattr(release, "original_year", None),
                "tmdb_id": release.movie.tmdb_id if release.movie else None,
                "tmdb_type": (release.movie.tmdb_type if release.movie else None) or release.type,
                "movie_name": movie_name,
                "boxset_name": boxset_name,
                "upc": release.upc,
                "asin": release.asin,
                "cover_front_url": release.cover_front_url,
                "cover_back_url": release.cover_back_url,
                "group_type": release.type,
            }
    )

    latest_job = (
        db.query(crud.models.Job)  # type: ignore[attr-defined]
        .filter(crud.models.Job.disc_id == disc.id)  # type: ignore[attr-defined]
        .order_by(crud.models.Job.created_at.desc())  # type: ignore[attr-defined]
        .first()
    )

    # Check if previews are still generating before allowing finalize
    if latest_job:
        disc_payload = latest_job.disc_payload or {}
        previews = disc_payload.get("previews", {})
        if isinstance(previews, dict):
            preview_status = previews.get("status")
            preview_tracks = previews.get("tracks", {})
            
            # Check if any previews are still generating
            if preview_tracks:
                has_generating = False
                has_failed = False
                for track_key, track_info in preview_tracks.items():
                    if isinstance(track_info, dict):
                        track_status = track_info.get("status")
                        if track_status in ("queued", "running"):
                            has_generating = True
                        elif track_status == "failed":
                            has_failed = True
                
                # If previews are still generating, prevent finalize
                if has_generating or preview_status in ("queued", "running"):
                    raise HTTPException(
                        409,
                        detail="Cannot finalize disc while previews are still generating. Please wait for preview generation to complete."
                    )
                
                # If previews failed, still allow finalize (user can proceed if they want)
                # But log a warning
                if has_failed or preview_status == "failed":
                    logger.warning(f"Finalizing disc {disc_id} with failed previews")

    # Ensure label stage is terminal before starting finalize to satisfy state dependencies.
    if latest_job:
        payload = latest_job.disc_payload or {}
        payload["label_ready"] = True
        payload["label_required"] = payload.get("label_required", True)
        latest_job.disc_payload = payload
        try:
            StageState.label_complete(
                db,
                latest_job,
                reason="label auto-completed for finalize",
                job_status=latest_job.job_status or "running",
            )
        except StateViolation as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    # Prefer persisted artifacts; fall back to the latest job folder (tmp or result).
    base_dir_str = None
    if latest_job:
        job_paths = JobPaths.for_id(str(latest_job.id))
        raw_dir = job_paths.raw
        if raw_dir.exists():
            base_dir_str = str(raw_dir)
    if not base_dir_str:
        artifacts = disc.artifacts or {}
        # Check for legacy result_location in artifacts
        base_dir_str = artifacts.get("result_location") if isinstance(artifacts, dict) else None
    if not base_dir_str:
        raise HTTPException(404, detail="No artifacts found to finalize")
    base_dir = Path(base_dir_str)
    if not base_dir.exists():
        raise HTTPException(404, detail="Artifacts path not found on disk")
    if latest_job and not getattr(disc, "artifacts", None):
        disc.artifacts = {
            "job_dir": str(JobPaths.for_id(str(latest_job.id)).root),
            "post_paths": getattr(latest_job, "post_paths", None),
            "final_hashes": (latest_job.disc_payload or {}).get("final_hashes") if latest_job.disc_payload else None,
        }
        db.commit()
        db.refresh(disc)
    
    # Generate expected output structure before finalize starts
    if latest_job:
        try:
            from core.stage_validation import generate_expected_finalize_output
            expected_output = generate_expected_finalize_output(latest_job, db)
            # Store expected output in disc_payload for validation later
            disc_payload = latest_job.disc_payload or {}
            disc_payload["expected_finalize_output"] = expected_output
            latest_job.disc_payload = disc_payload
            db.commit()
        except Exception as exc:
            logger.warning(f"Failed to generate expected finalize output: {exc}", exc_info=True)
    
    # Create checkpoint BEFORE state change so we can restore the previous state (after rip, before finalize)
    # We back up raw directory because that's where files are after rip completes, before finalize runs
    if latest_job:
        try:
            job_paths = JobPaths.from_job(latest_job)
            job_paths.ensure_layout()
            backup_dir = create_stage_backup(str(latest_job.id), "finalize", db, reason="before finalize stage")
            if backup_dir:
                # Back up raw directory (files after rip, before finalize)
                if job_paths.raw.exists() and any(job_paths.raw.glob("*.mkv")):
                    backup_files(job_paths.raw, backup_dir)
                    logger.info("Created checkpoint of raw directory and database state before finalization")
                else:
                    logger.warning("Skipped file backup - raw directory missing or no MKV files")
        except Exception as exc:
            logger.warning(f"Failed to create checkpoint for finalize: {exc}", exc_info=True)
            # Don't block finalization if backup fails

    if latest_job:
        try:
            apply_job_state(
                db,
                latest_job,
                updates={
                    "finalize_state": "running",
                    "phase": "postprocess",  # Set phase to postprocess instead of finalize for consistency
                    "job_status": latest_job.job_status or "running",
                },
                reason="disc finalize started",
            )
        except StateViolation as exc:
            raise HTTPException(409, detail=str(exc)) from exc

    # Generate finalize files and copy to job's finalize folder
    result = {
        "pending_release_finalize": True,
        "base_dir": str(base_dir),
        "label_payload": label_payload,
    }
    
    # Generate disc files directly in job's finalize folder
    try:
        if latest_job:
            job_paths = JobPaths.from_job(latest_job)
            job_paths.ensure_layout()
            
            # Generate disc files directly in job's finalize folder
            disc_files = discdb_finalize.generate_disc_files(
                base_dir,
                job_paths.finalize,
                label_payload,
                disc_hash=disc.content_hash,
            )
            
            result["job_finalize_dir"] = str(job_paths.finalize)
            logger.info("Generated finalize artifacts in job folder: %s", job_paths.finalize)
            
            # Validate finalize output
            try:
                from core.stage_validation import validate_finalize_output
                validation_result = validate_finalize_output(latest_job, db, job_paths)
                if not validation_result.valid:
                    errors_str = "; ".join(validation_result.errors)
                    logger.warning(f"Finalize validation failed: {errors_str}")
                    if validation_result.warnings:
                        warnings_str = "; ".join(validation_result.warnings)
                        logger.warning(f"Finalize validation warnings: {warnings_str}")
                else:
                    if validation_result.warnings:
                        warnings_str = "; ".join(validation_result.warnings)
                        logger.info(f"Finalize validation warnings: {warnings_str}")
                    logger.info("Finalize validation passed")
            except Exception as val_exc:
                logger.warning(f"Finalize validation error: {val_exc}", exc_info=True)
                # Don't fail finalization if validation check itself fails
        else:
            logger.warning("No job found for disc %s, skipping file generation", disc.id)
    except Exception as exc:
        logger.warning("Failed to generate finalize files for disc %s: %s", disc.id, exc)
        # Continue with basic result even if file generation fails

    if latest_job:
        # Only mark finalize_state as completed AFTER all work succeeds
        # This prevents marking as complete if an exception occurs after state update
        
        # First, ensure all disc-level updates succeed before updating job state
        try:
            disc.finalize_result = result
            disc.finalized = True
            disc.finalized_at = datetime.utcnow()
            db.commit()
            db.refresh(disc)
        except Exception as disc_exc:
            logger.error(f"Job {latest_job.id}: Failed to commit disc finalization: {disc_exc}", exc_info=True)
            # Rollback job state if disc commit fails
            try:
                apply_job_state(db, latest_job, updates={"finalize_state": "failed"}, reason="disc finalize commit failed")
            except Exception:
                pass
            raise HTTPException(500, detail=f"Failed to finalize disc: {disc_exc}") from disc_exc
        
        # Now update job state to completed (after disc is successfully finalized)
        # Note: Finalization is no longer part of the rip workflow pipeline.
        # It does not trigger postprocess - postprocess depends only on post_state being ready.
        updates: dict[str, Any] = {
            "finalize_state": "completed",
            "job_status": latest_job.job_status or "running",
        }
        
        try:
            apply_job_state(db, latest_job, updates=updates, reason="disc finalize completed")
            logger.info(f"Job {latest_job.id}: Finalize completed (finalization is separate from workflow pipeline)")
        except StateViolation as exc:
            logger.error(f"Job {latest_job.id}: State violation during finalize completion: {exc}")
            # Rollback disc state if job state update fails
            try:
                disc.finalized = False
                disc.finalize_result = None
                disc.finalized_at = None
                db.commit()
            except Exception:
                pass
            raise HTTPException(409, detail=str(exc)) from exc
        
        # Post-process is no longer auto-enqueued - user must manually trigger via POST /jobs/{job_id}/postprocess
    else:
        # No job case - still finalize the disc
        disc.finalize_result = result
        disc.finalized = True
        disc.finalized_at = datetime.utcnow()
        db.commit()
        db.refresh(disc)
    if release:
        # Mark release finalized when all discs finalized.
        if all(getattr(d, "finalized", False) or d.finalize_result for d in release.discs or []):
            release.finalized = True
            release.finalized_at = release.finalized_at or datetime.utcnow()
        db.refresh(release)
    return {"disc_id": disc.id, "finalize_result": result}


@router.post("/disc/{disc_id}/revert-finalization")
def revert_disc_finalization(disc_id: str, db: Session = Depends(get_db)):
    """
    DEV MODE ONLY: Revert disc finalization by restoring backup and resetting finalized state.
    This restores both file system state (finalize folder) and database state from backup.
    """
    if not is_dev_mode():
        raise HTTPException(403, detail="This endpoint is only available in dev mode (set ENABLE_DEVMODE=1)")


@router.post("/{slug}/finalize")
def finalize_release(slug: str, db: Session = Depends(get_db)):
    """
    Finalize a release after all discs are finalized.
    If any disc is not finalized, this returns 400 and does not change state.
    """
    rel = _get_release_by_id(db, slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    discs = rel.discs or []
    if not discs:
        raise HTTPException(400, detail="No discs for this release")
    not_finalized = [d.content_hash for d in discs if not d.finalize_result]
    if not_finalized:
        raise HTTPException(400, detail=f"Discs not finalized: {', '.join(not_finalized)}")

    # Build paths
    rel_type = (rel.type or "movie").strip().lower() or "movie"
    
    # Determine movie/boxset/series name
    movie_name = None
    boxset_name = None
    series_name = None
    
    if rel.movie and hasattr(rel.movie, "name") and rel.movie.name:
        movie_name = rel.movie.name.strip()
    elif rel.boxset and hasattr(rel.boxset, "name") and rel.boxset.name:
        boxset_name = rel.boxset.name.strip()
    # For series, we'd need to check if there's a series relationship
    # For now, fallback to release name if no movie/boxset found
    if not movie_name and not boxset_name:
        film_name = (rel.name or rel.slug).replace("/", "-").replace("\\", "-")
        if rel_type == "boxset":
            boxset_name = film_name
        elif rel_type in ("series", "tv"):
            series_name = film_name
        else:
            movie_name = film_name
    
    # Determine production year for movies
    production_year = None
    if rel.movie and hasattr(rel.movie, "production_year") and rel.movie.production_year:
        production_year = rel.movie.production_year
    elif hasattr(rel, "production_year") and rel.production_year:
        production_year = rel.production_year
    
    # Always use _film_dir() with the correct movie/boxset/series name to ensure correct directory structure.
    # Don't rely on fin_dir_hint from previously finalized discs as they may have the wrong structure.
    film_dir = discdb_finalize._film_dir(
        movie_name=movie_name,
        boxset_name=boxset_name,
        series_name=series_name,
        rel_type=rel_type,
        production_year=production_year,
    )
    current_dir = film_dir / (rel.id or rel.slug)
    target_dir = film_dir / rel.slug
    current_dir.mkdir(parents=True, exist_ok=True)
    if current_dir.exists() and current_dir != target_dir:
        if target_dir.exists():
            shutil.rmtree(target_dir)
        current_dir.rename(target_dir)

    # Generate disc outputs and release artifacts in the final target dir.
    first_disc = True
    for d in discs:
        fin = d.finalize_result or {}
        # Always rebuild the payload from the persisted disc/release state so titles/tracks reflect the DB,
        # not whatever was last posted from the client.
        label_payload = discdb_finalize.build_label_payload_from_disc(d, rel)
        latest_job = (
            db.query(crud.models.Job)  # type: ignore[attr-defined]
            .filter(crud.models.Job.disc_id == d.id)  # type: ignore[attr-defined]
            .order_by(crud.models.Job.created_at.desc())  # type: ignore[attr-defined]
            .first()
        )
        job_paths = JobPaths.from_job(latest_job) if latest_job else None
        if job_paths:
            try:
                job_paths.ensure_layout()
            except Exception as exc:
                logger.warning("Skipping job folder prep for finalize (job %s): %s", getattr(latest_job, "id", None), exc)
                job_paths = None

        # Find base_dir where makemkv_info.log is located (job's raw or metadata directory, not export directory)
        base_dir = None
        attempted_paths: list[str] = []
        
        # First, try to use JobPaths to find the job's raw directory (where makemkv_info.log should be)
        if job_paths:
            # Check raw directory first (most common location for makemkv_info.log)
            raw_dir = job_paths.raw
            if raw_dir.exists():
                base_dir = str(raw_dir)
                attempted_paths.append(str(raw_dir))
            # Fallback to metadata directory
            if not base_dir:
                metadata_dir = job_paths.metadata
                if metadata_dir.exists():
                    base_dir = str(metadata_dir)
                    attempted_paths.append(str(metadata_dir))
        
        # Fallback to computed job raw directory
        if not base_dir and latest_job:
            raw_dir = JobPaths.for_id(str(latest_job.id)).raw
            if raw_dir.exists():
                base_dir = str(raw_dir)
                attempted_paths.append(str(raw_dir))
        
        # Fallback to disc artifacts
        if not base_dir:
            artifacts = getattr(d, "artifacts", {}) or {}
            base_dir = artifacts.get("result_location")
            if base_dir:
                attempted_paths.append(str(base_dir))
        
        # Last resort: try base_dir from finalize_result (but not release_dir, as that's the export directory)
        if not base_dir and isinstance(fin, dict):
            base_dir = fin.get("base_dir")  # Only use base_dir, not release_dir
            if base_dir:
                attempted_paths.append(str(base_dir))
        
        if not base_dir:
            raise HTTPException(404, detail=f"No artifacts found for disc {d.id}")
        base_path = Path(base_dir)
        if not base_path.exists():
            msg = f"Artifacts path not found on disk for disc {d.id}"
            if attempted_paths:
                msg += f" (tried: {', '.join(attempted_paths)})"
            if latest_job:
                msg += f"; latest_job_id={latest_job.id}"
            raise HTTPException(404, detail=msg)

        # Generate release-level files (release.json, etc.) only for first disc
        # Disc files will be copied from job finalize folders for all discs
        if first_disc:
            info_log = base_path / "makemkv_info.log"
            if not info_log.exists():
                fallback_dir = Path(fin.get("release_dir")) if isinstance(fin, dict) and fin.get("release_dir") else None
                if fallback_dir and fallback_dir.exists():
                    res = {"release_dir": str(fallback_dir)}
                else:
                    raise HTTPException(404, detail=f"makemkv_info.log not found for disc {d.id}")
            else:
                # Use the movie/boxset/series names already determined at the top of the function
                # Add movie/boxset names to label_payload
                label_payload["movie_name"] = movie_name
                label_payload["boxset_name"] = boxset_name
                label_payload["series_name"] = series_name
                
                res = discdb_finalize.finalize_from_label(
                    base_path,
                    label_payload,
                    disc_hash=d.content_hash,
                    release_type=rel.type,
                    release_name=rel.name,
                    release_slug_override=rel.slug,
                    write_release_artifacts=True,  # Generate release.json and release-level files
                    write_film_metadata=True,
                )
        else:
            # For subsequent discs, use the release_dir from first disc
            res = {"release_dir": str(target_dir)}
        
        # Copy disc files from job finalize folder to release export folder
        if job_paths:
            release_dir = res.get("release_dir") if isinstance(res, dict) else None
            if not release_dir:
                release_dir = str(target_dir)
            release_dir_path = Path(release_dir)
            release_dir_path.mkdir(parents=True, exist_ok=True)
            
            # Copy disc-specific files from job's finalize folder to release export folder
            disc_number = d.disc_number or 1
            base_name = f"disc{disc_number:02d}"
            for pattern in [f"{base_name}.json", f"{base_name}-summary.txt", f"{base_name}.txt"]:
                src_file = job_paths.finalize / pattern
                if src_file.exists():
                    dest_file = release_dir_path / pattern
                    shutil.copy2(src_file, dest_file)
                    logger.debug("Copied %s from job finalize to release export", pattern)
                else:
                    logger.warning("Disc file not found in job finalize folder: %s", src_file)
            
            if isinstance(res, dict):
                res["release_dir"] = str(release_dir_path)
        
        d.finalize_result = res
        d.finalized = True
        d.finalized_at = datetime.utcnow()
        first_disc = False
        db.add(d)

    # Film metadata: copy from discdb repo if present else keep what finalize_from_label wrote.
    # Determine film_name from whichever variable was set (movie_name, boxset_name, or series_name)
    film_name = movie_name or boxset_name or series_name
    if not film_name:
        # Fallback to release name if nothing was set
        film_name = (rel.name or rel.slug).replace("/", "-").replace("\\", "-")
    
    # Determine film_year from release or movie/boxset
    film_year = None
    if rel.movie and hasattr(rel.movie, "production_year") and rel.movie.production_year:
        film_year = rel.movie.production_year
    elif hasattr(rel, "production_year") and rel.production_year:
        film_year = rel.production_year
    elif hasattr(rel, "release_year") and rel.release_year:
        film_year = rel.release_year
    
    repo_root = Path("/tmp/thediscdb-data/data")
    repo_film_name = f"{film_name}{(' (' + str(film_year) + ')') if film_year else ''}"
    repo_film_dir = repo_root / rel_type / repo_film_name
    if repo_film_dir.exists():
        for fname in ("cover.jpg", "tmdb.json", "metadata.json", "imdb.json"):
            discdb_finalize._safe_copy(repo_film_dir / fname, film_dir / fname)

    rel.finalize_state = "completed"
    rel.finalized = True
    rel.finalized_at = rel.finalized_at or datetime.utcnow()

    # Mark latest jobs for each disc as finalize_release completed and, if all stages done, job_status completed.
    for d in discs:
        latest_job = (
            db.query(crud.models.Job)  # type: ignore[attr-defined]
            .filter(crud.models.Job.disc_id == d.id)  # type: ignore[attr-defined]
            .order_by(crud.models.Job.created_at.desc())  # type: ignore[attr-defined]
            .first()
        )
        if latest_job:
            updates: dict[str, Any] = {
                "finalize_release_state": "completed",
            }
            if latest_job.transfer_state in ("completed", "skipped"):
                updates["job_status"] = "completed"
                updates["phase"] = "complete"
                # Backfill required stage states so completion invariants can be enforced.
                # #365 step 4 — no more post_state backfill. The validation
                # at _validate_completed_invariant reads derived_post_state
                # which returns "completed" once transfer_state="completed"
                # is set (same logic as the analogous cleanup in #468's
                # _complete_transfer / bg-transfer-complete paths).
                profile = (getattr(latest_job, "stage_profile", None) or "miss").lower()
                updates.setdefault("rip_state", getattr(latest_job, "rip_state", None) or ("completed" if (getattr(latest_job, "rip_progress", 0) or 0) >= 100 else None))
                updates.setdefault("transfer_state", getattr(latest_job, "transfer_state", None) or "completed")
                if profile == "hit":
                    updates.setdefault("label_state", getattr(latest_job, "label_state", None) or "skipped")
                    updates.setdefault("finalize_state", getattr(latest_job, "finalize_state", None) or "skipped")
                else:
                    updates.setdefault("label_state", getattr(latest_job, "label_state", None) or "completed")
                    updates.setdefault("finalize_state", getattr(latest_job, "finalize_state", None) or "completed")
            else:
                updates["phase"] = "complete"  # Set phase to complete instead of finalize_release for consistency
            # Bulk finalize should be transactional; validate first, then mutate in-memory and commit once.
            try:
                normalized = normalize_state_updates(updates)
                validate_job_state_transition(latest_job, normalized)
                for k, v in normalized.items():
                    setattr(latest_job, k, v)
                db.add(latest_job)
            except Exception as exc:
                raise HTTPException(409, detail=f"Invalid job state transition for job {latest_job.id}: {exc}") from exc
    db.commit()
    db.refresh(rel)

    return {
        "release": ReleaseSummary(
            id=rel.id,
            slug=rel.slug,
            type=rel.type,
            name=rel.name,
            title=rel.name,
            tmdb_id=rel.movie.tmdb_id if rel.movie else None,
            upc=rel.upc,
            asin=rel.asin,
            cover_front_url=rel.cover_front_url,
            cover_back_url=rel.cover_back_url,
            finalize_state=rel.finalize_state,
            total_discs=len(discs),
            completed_discs=len(discs),
            finalized_discs=len(discs),
        ),
        "metadata_dir": str(target_dir),
    }


@router.get("/disc/by-hash")
def get_disc_by_hash(
    content_hash: str | None = None,
    disc_id: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Resolve a disc (and its release summary) by content hash or disc_id.
    Frontend now prefers disc_id (from discinfo) but hash is kept for backward compatibility.
    """
    disc = None
    if disc_id:
        disc = (
            db.query(crud.models.Disc)  # type: ignore[attr-defined]
            .options(
                joinedload(crud.models.Disc.titles),
                joinedload(crud.models.Disc.title_streams),
                joinedload(crud.models.Disc.release).joinedload(db_models.Release.boxset),  # type: ignore
            )
            .filter(crud.models.Disc.id == disc_id)  # type: ignore
            .first()
        )
    if not disc and content_hash:
        disc = (
            db.query(crud.models.Disc)  # type: ignore[attr-defined]
            .options(
                joinedload(crud.models.Disc.titles),
                joinedload(crud.models.Disc.title_streams),
                joinedload(crud.models.Disc.release).joinedload(db_models.Release.boxset),  # type: ignore
            )
            .filter(crud.models.Disc.content_hash == content_hash)  # type: ignore
            .first()
        )
    if not disc:
        if disc_id and not content_hash:
            raise HTTPException(404, detail="Disc not found")
        if not content_hash:
            raise HTTPException(400, detail="content_hash or disc_id required")
        # Read-only: do not create a disc for unknown content_hash; frontend uses disc_id only.
        raise HTTPException(404, detail="Disc not found")
    latest_job = sorted(disc.jobs, key=lambda j: j.created_at)[-1] if disc.jobs else None
    rel = disc.release
    discs = rel.discs if rel else []
    disc_rec = _disc_record(disc)
    # #528: DiscSummary.titles is the typed TitleSummary projection since
    # #380/#500 — reuse the Library's projection helper instead of dumping
    # DiscTitleRecord dicts (keyed `id`, which fails TitleSummary validation).
    disc_titles = _build_title_summaries(getattr(disc, "titles", None))
    title_stream_payload = (
        [t.model_dump(exclude_none=True) for t in disc_rec.title_streams] if disc_rec.title_streams else []
    )
    # Fallback: with no persisted titles, hydrate raw scan tracks from the
    # latest job payload. Those dicts are MakeMKV-shaped, not TitleSummary —
    # they belong on DiscSummary.tracks (the documented disc_payload-tracks
    # field), never on the typed `titles`.
    scan_tracks = []
    if not disc_titles:
        try:
            latest_payload = (latest_job.disc_payload or {}) if latest_job else {}
        except Exception:
            latest_payload = {}
        try:
            st = latest_payload.get("scan_tracks") or latest_payload.get("tracks") or latest_payload.get("titles") or []
            if isinstance(st, dict):
                scan_tracks = list(st.values())
            elif isinstance(st, list):
                scan_tracks = st
        except Exception:
            scan_tracks = []
        scan_tracks = [t for t in scan_tracks if isinstance(t, dict)]
    return {
        "disc": DiscSummary(
            id=disc.id,
            content_hash=disc.content_hash,
            release_id=rel.id if rel else None,
            release_slug=rel.slug if rel else None,
            disc_number=disc.disc_number,
            discdb_disc_num=getattr(disc, "discdb_disc_num", None),
            disc_slug=disc.disc_slug,
            disc_name=disc.disc_name,
            format=disc.format,
            label_present=bool(disc_rec.titles or disc.titles or disc_rec.title_streams or disc.title_streams),
            finalized=bool(getattr(disc, "finalized", False) or disc.finalize_result),
            latest_job_id=str(latest_job.id) if latest_job else None,
            latest_job_status=latest_job.job_status if latest_job else None,
            latest_job_progress=latest_job.rip_progress if latest_job else None,
            transfer_state=getattr(latest_job, "transfer_state", None) if latest_job else None,
            titles=disc_titles,
            title_streams=title_stream_payload or None,
            tracks=scan_tracks or None,
        ),
        "release": _release_summary(rel, db) if rel else None,
    }


@router.get("/disc/by-id")
def get_disc_by_id(disc_id: str, db: Session = Depends(get_db)):
    """
    Convenience wrapper to fetch disc/release by disc_id.
    """
    return get_disc_by_hash(content_hash=None, disc_id=disc_id, db=db)  # type: ignore[arg-type]


@router.delete("/{slug}")
def delete_release(slug: str, db: Session = Depends(get_db)):
    """
    Delete a release, its discs, jobs, and related transient files.
    """
    rel = _get_release_by_id(db, slug)
    if not rel:
        raise HTTPException(404, detail="Release not found")
    discs = list(rel.discs or [])
    total_jobs = 0
    for disc in discs:
        jobs = list(disc.jobs or [])
        total_jobs += len(jobs)
        for job in jobs:
            _cleanup_job_files(job)
            db.delete(job)
        db.delete(disc)
    db.delete(rel)
    db.commit()
    return {"deleted_release": slug, "deleted_discs": len(discs), "deleted_jobs": total_jobs}


# ────────────────────────────────────────────────────────────────
# Rename endpoint: re-rename title files after metadata edits
# ────────────────────────────────────────────────────────────────

@router.post("/disc/{disc_id}/rename", response_model=RenameResponse)
def rename_disc_titles(
    disc_id: str,
    dry_run: bool = Query(default=True),
    db: Session = Depends(get_db),
):
    """Re-rename title files on disk to match current metadata (#325).

    If dry_run=True (default), returns a preview mapping without moving files.
    If dry_run=False, renames files and updates file_path on DiscTitle rows.

    Response shape is documented in :class:`api.schemas.RenameResponse`.
    Errors: 404 disc not found · 400 disc has no release · 409 transfer in
    progress. Per-title problems (collision / missing / error) come back as
    rows in ``results`` with the corresponding ``status``, not as HTTP errors.
    """
    from core.disc import compute_expected_path, rename_title_file
    from core import settings as app_settings

    disc = (
        db.query(db_models.Disc)
        .options(
            joinedload(db_models.Disc.titles),
            joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
        )
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        raise HTTPException(404, detail="Disc not found")

    release = disc.release
    if not release:
        raise HTTPException(400, detail="Disc has no release — cannot compute paths")
    movie = release.movie

    release_type = (release.type or "movie").strip().lower()
    movie_name = movie.name if movie else ""
    production_year = movie.production_year if movie else None
    release_name = release.name or ""
    media_server = app_settings.get_media_server()
    resolution = release.resolution

    # Check if any active transfer is in progress
    active_job = (
        db.query(db_models.Job)
        .filter(db_models.Job.disc_id == disc_id)
        .filter(db_models.Job.transfer_state.in_(["running", "pending"]))
        .first()
    )
    if active_job:
        raise HTTPException(409, detail="Cannot rename — transfer is in progress")

    results: list[dict] = []
    for title in disc.titles:
        if not title.file_path:
            continue
        if title.type and title.type.strip().lower() == "ignore":
            continue

        title_meta = {
            "title": title.title,
            "type": _normalize_title_type(title.type),
            "season": title.season,
            "episode": title.episode,
            "edition": title.edition,
            "description": title.description,
        }
        release_meta = {"release_type": release_type, "release_name": release_name}
        movie_meta = {"movie_name": movie_name, "production_year": production_year}

        per_title_res = resolution
        if title.metadata_scan and isinstance(title.metadata_scan, dict):
            scan_res = title.metadata_scan.get("resolution")
            if scan_res:
                per_title_res = scan_res

        expected_rel = compute_expected_path(
            title_meta, release_meta, movie_meta,
            media_server=media_server, resolution=per_title_res,
        )

        # Compute absolute new path based on the stage
        old_path = title.file_path
        stage = title.file_path_stage or "postprocess"
        if stage == "transfer":
            # For transferred files, the expected relative path starts with type_dir;
            # replace the current filename structure under the same root.
            old_dir = os.path.dirname(old_path)
            # Walk up to find the type_dir root (Movies/Series)
            parts = Path(old_path).parts
            type_idx = None
            for i, p in enumerate(parts):
                if p in ("Movies", "Series"):
                    type_idx = i
                    break
            if type_idx is not None:
                base = str(Path(*parts[:type_idx]))
                new_path = os.path.join(base, expected_rel)
            else:
                new_path = os.path.join(old_dir, os.path.basename(expected_rel))
        else:
            # For rip/postprocess, resolve relative to the job's transient root
            parts = Path(old_path).parts
            type_idx = None
            for i, p in enumerate(parts):
                if p in ("Movies", "Series"):
                    type_idx = i
                    break
            if type_idx is not None:
                base = str(Path(*parts[:type_idx]))
                new_path = os.path.join(base, expected_rel)
            else:
                new_path = os.path.join(os.path.dirname(old_path), os.path.basename(expected_rel))

        entry = {
            "title_id": title.id,
            "old_path": old_path,
            "new_path": new_path,
            "changed": os.path.abspath(old_path) != os.path.abspath(new_path),
            "status": "preview",
        }

        # Collision detection
        if entry["changed"] and os.path.exists(new_path):
            if os.path.exists(old_path) and os.path.samefile(old_path, new_path):
                entry["changed"] = False
            else:
                entry["status"] = "collision"
                entry["error"] = f"Destination already exists: {new_path}"

        if not dry_run and entry["changed"] and entry["status"] != "collision":
            if not os.path.exists(old_path):
                entry["status"] = "missing"
                entry["error"] = f"Source file not found: {old_path}"
            else:
                move_result = rename_title_file(old_path, new_path)
                if move_result["success"]:
                    title.file_path = new_path
                    entry["status"] = "renamed"
                else:
                    entry["status"] = "error"
                    entry["error"] = move_result.get("error")

        results.append(entry)

    if not dry_run:
        db.commit()

    return {
        "disc_id": disc_id,
        "dry_run": dry_run,
        "results": results,
    }


# ────────────────────────────────────────────────────────────────────────
# #449 — Self-healing library reattach
# ────────────────────────────────────────────────────────────────────────


@router.post("/library/reattach", response_model=LibraryReattachReport)
def library_reattach(
    transfer_config_id: Optional[str] = None,
    dry_run: bool = True,
    db: Session = Depends(get_db),
):
    """Walk the active transfer destination and reattach on-disk MKVs to
    their ``DiscTitle`` rows by Matroska Segment UID (#449).

    Solves the "I imported a previous DB export onto a fresh install"
    case (and the "I moved files in Plex" case) without re-rip:

      1. Resolve the TransferConfig's ``transfer_dir`` to a walkable path.
      2. ``Path(transfer_dir).rglob("*.mkv")`` for candidates.
      3. **Primary match** — call :func:`core.mkv_identity.read_segment_uid`
         on each candidate; if a UID matches a ``DiscTitle.segment_uid``,
         that's a deterministic match.
      4. **Heuristic fallback** — for titles whose ``segment_uid IS NULL``
         (legacy rows from before PR #451), match on filename
         (``DiscTitle.file_path`` basename or the file's basename matches
         the title's ``source_file``).
      5. Returns a structured report; if ``dry_run=False``, applies the
         matches via :func:`workers.tasks._update_title_file_paths` and
         re-returns the same report shape with ``applied=True``.

    Conflicts (one file matching multiple titles) are reported but never
    applied — the operator decides.

    Args:
      transfer_config_id: Optional override; defaults to the active config.
      dry_run: True (default) returns the report without writing. Set
        False to apply matches.

    Returns:
      LibraryReattachReport with deterministic_matches, heuristic_matches,
      orphan_files, orphan_titles, conflicts, and the walked transfer_dir.

    Errors:
      * 400 — no active config / transfer_dir empty / not local-walkable
        (smb:// without a local mount)
      * 404 — transfer_config_id given but not found
    """
    # Resolve the config.
    if transfer_config_id:
        config = (
            db.query(db_models.TransferConfig)
            .filter(db_models.TransferConfig.id == transfer_config_id)
            .first()
        )
        if config is None:
            raise HTTPException(404, detail=f"TransferConfig {transfer_config_id} not found")
    else:
        from core.transfer.service import get_active_config
        config = get_active_config(db)
        if config is None:
            raise HTTPException(
                400,
                detail="No active TransferConfig. Pass transfer_config_id explicitly "
                       "or set an active config in Settings → Transfer Configs.",
            )

    # We can only walk a local filesystem path. SMB / NFS / rsync URIs are
    # out of scope until the user mounts the share locally (the issue's
    # explicit Phase 3 deferral).
    if (getattr(config, "mode", None) or "").lower() != "local":
        raise HTTPException(
            400,
            detail=f"Reattach can only walk local-mode transfer destinations "
                   f"(active config mode={config.mode!r}). Mount the share "
                   f"locally and create a local-mode TransferConfig pointing "
                   f"at the mount point to proceed.",
        )

    transfer_dir_str = (getattr(config, "transfer_dir", None) or "").strip()
    if not transfer_dir_str:
        raise HTTPException(
            400,
            detail="TransferConfig.transfer_dir is empty. Set the Transfer Path "
                   "in Settings → Transfer Configs.",
        )

    transfer_dir = Path(transfer_dir_str).resolve()
    if not transfer_dir.exists():
        raise HTTPException(
            400,
            detail=f"Transfer destination not found on disk: {transfer_dir}",
        )

    return _build_library_reattach_report(transfer_dir, db, dry_run=dry_run)


def _build_library_reattach_report(
    transfer_dir: Path, db: Session, *, dry_run: bool
) -> LibraryReattachReport:
    """Walk ``transfer_dir`` and produce a reattach report. Applies the
    matches when ``dry_run=False``. Extracted from the endpoint so tests
    can drive it without HTTP overhead.

    Match precedence (deterministic before heuristic, both reported):
      * **segment_uid** — the trust path (~unique per MKV produced by
        MakeMKV; survives moves/renames; populated by PR #451).
      * **filename** — fallback for legacy rows; matches the on-disk
        file's basename against ``DiscTitle.source_file`` or the basename
        of a previously-recorded ``DiscTitle.file_path``.

    Multi-candidate matches go to ``conflicts`` and are skipped on the
    write path — the operator must disambiguate (re-rip, manual file
    rename, or a future per-conflict resolution UI).
    """
    from core.mkv_identity import read_segment_uid
    from workers.tasks import _update_title_file_paths

    # Candidate files on disk.
    candidate_files = sorted(transfer_dir.rglob("*.mkv"))

    # All disc titles in the DB. We'll bucket by segment_uid and by
    # basename(source_file) for two-pass matching.
    titles = db.query(db_models.DiscTitle).all()
    titles_by_uid: Dict[str, List[db_models.DiscTitle]] = {}
    titles_by_filename: Dict[str, List[db_models.DiscTitle]] = {}
    for t in titles:
        uid = (getattr(t, "segment_uid", None) or "").strip()
        if uid:
            titles_by_uid.setdefault(uid, []).append(t)
        # Heuristic key: basename of source_file (e.g. "00504.mpls") or the
        # basename of a previously-recorded absolute file_path. Both can be
        # present; both feed the same heuristic bucket so the matcher tries
        # them in parallel.
        for cand in (
            os.path.basename(getattr(t, "file_path", None) or ""),
            getattr(t, "source_file", None) or "",
        ):
            cand = (cand or "").strip()
            if cand:
                titles_by_filename.setdefault(cand, []).append(t)

    deterministic_matches: List[LibraryReattachMatch] = []
    heuristic_matches: List[LibraryReattachMatch] = []
    orphan_files: List[str] = []
    conflicts: List[LibraryReattachConflict] = []
    matched_title_ids: set[str] = set()

    # Per-disc reattach buffers for the wet-run write. Keyed by disc_id;
    # _update_title_file_paths takes a single disc_id at a time.
    pending_writes: Dict[str, Dict[str, str]] = {}

    for f in candidate_files:
        f_path = str(f)
        f_name = f.name

        # 1) Primary: segment_uid match.
        uid = read_segment_uid(f_path)
        candidates: List[db_models.DiscTitle] = []
        tier: str = ""
        if uid:
            candidates = titles_by_uid.get(uid, [])
            if candidates:
                tier = "segment_uid"

        # 2) Heuristic: filename match (only when segment_uid didn't resolve).
        if not candidates:
            candidates = titles_by_filename.get(f_name, [])
            if candidates:
                tier = "filename"

        if not candidates:
            orphan_files.append(f_path)
            continue

        # Drop already-matched titles so a duplicate UID at a second on-disk
        # file doesn't reattach to the same title twice — that's a conflict
        # the operator should see.
        candidates = [t for t in candidates if str(t.id) not in matched_title_ids]
        if not candidates:
            # Title(s) for this file already claimed by an earlier candidate
            # — surface as a conflict so the operator notices the dup.
            conflicts.append(
                LibraryReattachConflict(
                    file_path=f_path,
                    candidate_title_ids=[],
                    tier=tier,
                )
            )
            continue

        if len(candidates) > 1:
            conflicts.append(
                LibraryReattachConflict(
                    file_path=f_path,
                    candidate_title_ids=[str(t.id) for t in candidates],
                    tier=tier,
                )
            )
            continue

        chosen = candidates[0]
        match = LibraryReattachMatch(
            title_id=str(chosen.id),
            old_path=getattr(chosen, "file_path", None),
            new_path=f_path,
            tier=tier,
        )
        if tier == "segment_uid":
            deterministic_matches.append(match)
        else:
            heuristic_matches.append(match)
        matched_title_ids.add(str(chosen.id))
        # Build the per-disc rel-path map for _update_title_file_paths. The
        # helper takes (disc_id, {title_id: rel_path}, stage, base_dir);
        # base_dir is transfer_dir and rel_path is the file's path relative
        # to it.
        try:
            rel = str(f.relative_to(transfer_dir))
        except ValueError:
            rel = f_path
        pending_writes.setdefault(str(chosen.disc_id), {})[str(chosen.id)] = rel

    # Titles that nothing on disk matched. Reported so the operator knows
    # the wipe-and-reimport didn't fully heal everything.
    orphan_titles = [
        str(t.id) for t in titles if str(t.id) not in matched_title_ids
    ]

    applied = False
    if not dry_run and pending_writes:
        for disc_id, path_map in pending_writes.items():
            _update_title_file_paths(
                db, disc_id, path_map, "transfer", base_dir=str(transfer_dir)
            )
        db.commit()
        applied = True

    return LibraryReattachReport(
        deterministic_matches=deterministic_matches,
        heuristic_matches=heuristic_matches,
        orphan_files=orphan_files,
        orphan_titles=orphan_titles,
        conflicts=conflicts,
        transfer_dir=str(transfer_dir),
        dry_run=dry_run,
        applied=applied,
    )
