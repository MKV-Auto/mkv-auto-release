# api/routers/jobs.py

import logging, json, datetime, uuid, time
import requests
from typing import List, Optional, Dict, Any, Literal, Callable
from pathlib import Path
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Body, Query, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session, joinedload, selectinload, object_session
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
import os, shutil

from api import crud, database
from api import models as db_models
from pydantic import BaseModel, ValidationError, model_validator
from api.schemas import (
    JobCreate,
    JobResponse,
    JobStatus,
    StepCompleteRequest,
    CurrentJobResponse,
    DiscDetail,
    JobArtifacts,
    JobListItem,
    TransferRequest,
    LabelRequest,
    LabelUpdate,
    ReleaseProgress,
    PreviewInfo,
    ValidationResult,
    WorkflowContextResponse,
    WorkflowContextUpdate,
    MovieSummary,
    BoxsetSummary,
    ReleaseSummary,
)
from workers.tasks import (
    rip_disc,
    enqueue_rip_verification_for_job,
    generate_previews,
    generate_preview_track,
    preview_raw_titles,
    detect_raw_titles,
    cleanup_job_mkv,
)
from core.utils import is_dev_mode
from core.title_type_normalize import normalize_title_type_for_api as _normalize_title_type
from core.job_paths import JobPaths
from core.preview_recovery import (
    PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS,
    active_generate_previews_job_ids,
    build_preview_regeneration_state,
    user_reset_preview_auto_recovery_metadata,
)
from core.transfer import service as transfer_service
from core.transfer.utils import error_handler as transfer_error_handler
from core.transfer import monitoring as transfer_cleanup
from core import importbuddy_prefill
from core.disc import Disc
from core.drive_manager_client import validate_disc_info, DriveManagerError
from core.drive_gatekeeper import DriveGatekeeper, is_pid_alive
from core.duplicate_info import attach_duplicate_info
try:
    from drive_manager.state import get_drive_state as _get_drive_state
except Exception as _drive_state_exc:  # pragma: no cover - only when drive_manager not installed
    _drive_state_logger = logging.getLogger("api.routers.jobs")

    def _get_drive_state():  # type: ignore[no-redef]
        _drive_state_logger.warning("Drive manager unavailable: %s", _drive_state_exc)
        return None
from core.utils import (
    get_mkvauto_data, build_release_slug, get_export_root, is_dev_mode,
    calculate_required_rip_space_bytes, check_disk_space_for_rip,
    resolve_jobs_root
)
from core import discdb_finalize
from core.job_state import (
    StateViolation,
    StageState,
    _infer_profile,
    apply_job_state,
    apply_job_state_devmode,
    normalize_state_updates,
    validate_job_state_transition,
)
from core.stage_backup import create_stage_backup, backup_files, restore_stage_backup, restore_files, get_stage_backup_dir

transfer_log = logging.getLogger("transfer")

router = APIRouter(prefix="/jobs", tags=["jobs"])
log = logging.getLogger("api.routers.jobs")


def _workflow_profile_for_steps(job: Any) -> str:
    """Prefer stage_profile for workflow branching; fall back to discdb_result for legacy rows."""
    sp = getattr(job, "stage_profile", None)
    if isinstance(sp, str) and sp.strip():
        return sp.strip().lower()
    dr = getattr(job, "discdb_result", None)
    if isinstance(dr, str) and dr.strip():
        return dr.strip().lower()
    return "miss"


def _workflow_discdb_hit_for_context(job: Any, disc_payload: dict | None = None) -> bool:
    """True when the job follows the short hit workflow (aligns with frontend stage_profile logic)."""
    payload = disc_payload if disc_payload is not None else (getattr(job, "disc_payload", None) or {})
    sp = getattr(job, "stage_profile", None)
    if sp == "hit" or sp == "miss":
        return sp == "hit"
    dr = getattr(job, "discdb_result", None)
    if dr == "hit" or dr == "miss":
        return dr == "hit"
    if payload:
        has_hash = bool(payload.get("disc_hash") or payload.get("content_hash"))
        return has_hash and not bool(payload.get("label_required"))
    return False
STALE_JOB_TIMEOUT_SECONDS = int(os.getenv("STALE_JOB_TIMEOUT_SECONDS", "900"))  # default 15 minutes
# Do not treat Celery inspect misses as orphan for recent STARTED/PROGRESS rips (inspect is unreliable).
RIP_ORPHAN_INSPECT_GRACE_SECONDS = int(os.getenv("RIP_ORPHAN_INSPECT_GRACE_SECONDS", "180"))


def _job_disc_num_for_rip_lock(job: Any) -> Optional[str]:
    dn = getattr(job, "disc_num", None)
    if dn is not None and str(dn).strip() != "":
        return str(dn)
    payload = getattr(job, "disc_payload", None) or {}
    if isinstance(payload, dict):
        raw = payload.get("disc_num")
        if raw is not None and str(raw).strip() != "":
            return str(raw)
    return None


def _rip_operation_lock_held_for_job(job: Any) -> bool:
    """True if unified disc rip lock is held (worker in rip_disc before/during makemkv)."""
    disc_num = _job_disc_num_for_rip_lock(job)
    if not disc_num:
        return False
    try:
        from core.disc_locks import OPERATION_RIP, is_operation_active

        return is_operation_active(disc_num, OPERATION_RIP)
    except Exception as exc:
        log.debug("_rip_operation_lock_held_for_job failed: %s", exc)
        return False


def _rip_orphan_inspect_within_grace(job: Any, task_state: str) -> bool:
    if RIP_ORPHAN_INSPECT_GRACE_SECONDS <= 0:
        return False
    if task_state not in ("STARTED", "PROGRESS"):
        return False
    updated = getattr(job, "updated_at", None)
    if updated is None:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc) - updated).total_seconds()
    return age < float(RIP_ORPHAN_INSPECT_GRACE_SECONDS)


def get_db():
    db = database.SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_callback_client_host(request: Request) -> str:
    """Return client host for callback endpoints (localhost-only). Tests override to 127.0.0.1."""
    return request.client.host if request.client else ""


def _advance_transfer_phase(db: Session, job: Any, new_phase: str, *, reason: str) -> None:
    """Idempotently advance ``Job.transfer_phase`` (#365 sub-phase indicator).

    Sub-phase progression: ``"preparing"`` (set by ``start_transfer``'s
    prep body) → ``"transferring"`` (set when the actual file
    movement begins) → ``"verifying"`` (set on first hash-progress
    callback when post-transfer verification starts). The frontend's
    ``transferPhaseLabel`` reads this column to render the user-facing
    sub-phase label under the Transfer step.

    Idempotent: if the column already matches ``new_phase``, the call
    is a no-op. Exception-safe: a transient DB hiccup logs a warning
    and continues — we never want a state-write failure to crash an
    in-flight transfer of multi-GB content.
    """
    if getattr(job, "transfer_phase", None) == new_phase:
        return
    try:
        apply_job_state(
            db, job,
            updates={"transfer_phase": new_phase},
            reason=reason,
            skip_context_changed=True,
        )
    except Exception as exc:
        log.warning("Job %s: failed to advance transfer_phase to %s: %s", getattr(job, "id", "?"), new_phase, exc)


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


def _derive_pipeline(job) -> tuple[dict, str]:
    """
    Build the pipeline map based on stored stage states and profile.
    """
    from core.job_state import _infer_profile
    
    disc_payload = getattr(job, "disc_payload", None) or {}
    profile = _infer_profile(job)  # Use centralized profile inference
    job_status = (getattr(job, "job_status", None) or "pending").lower()
    pipeline: dict[str, str] = {}

    def resolve_state(stored: str | None, default: str) -> str:
        if stored in ("pending", "ready", "running", "completed", "failed", "skipped"):
            return stored
        return default

    rip_progress = getattr(job, "rip_progress", 0) or 0
    rip_state = resolve_state(getattr(job, "rip_state", None), "pending")
    if job_status == "failed":
        rip_state = "failed" if rip_state not in ("completed",) else rip_state
    elif job_status in ("completed",) and rip_state not in ("completed",):
        rip_state = "completed"
    elif rip_progress > 0 and rip_state == "pending":
        rip_state = "running"
    pipeline["rip"] = rip_state

    label_state = resolve_state(getattr(job, "label_state", None), "pending" if profile == "miss" else "skipped")
    finalize_state = resolve_state(getattr(job, "finalize_state", None), "pending" if profile == "miss" else "skipped")
    # #365 — derived_post_state is the source of truth (post_state column is
    # being dropped over several PRs; see docs/plans/postprocess-collapse-handoff.md).
    post_state = resolve_state(job.derived_post_state, "pending")
    transfer_state = resolve_state(getattr(job, "transfer_state", None), "pending")
    finalize_release_state = resolve_state(
        getattr(job, "finalize_release_state", None),
        "pending" if profile == "miss" else "skipped",
    )

    if profile == "hit":
        pipeline["label"] = "skipped"
        pipeline["finalize"] = "skipped"
        pipeline["postprocess"] = post_state
        pipeline["transfer"] = transfer_state
        pipeline["finalize_release"] = "skipped"
    else:
        pipeline["label"] = label_state
        pipeline["finalize"] = finalize_state
        pipeline["postprocess"] = post_state
        pipeline["transfer"] = transfer_state
        pipeline["finalize_release"] = finalize_release_state

    stage_order = [
        "rip",
        "label" if profile == "miss" else None,
        "postprocess",
        "transfer",
        "finalize_release" if profile == "miss" else None,
    ]
    stage_order = [s for s in stage_order if s]

    # prefer explicit failure/success first
    if job_status == "failed":
        return pipeline, "failed"
    if job_status == "completed":
        return pipeline, "complete"

    stored_phase = (getattr(job, "phase", None) or "").lower()
    
    # Check for failed rip first - this should fail the whole job
    if rip_state == "failed":
        return pipeline, "failed"
    
    # Check for any failed stage before falling back to stored phase
    if any(state == "failed" for state in pipeline.values()):
        return pipeline, "failed"
    
    # Iterate through stages in order
    for key in stage_order:
        state = pipeline.get(key)
        if state in ("running", "pending", "ready"):
            return pipeline, key
        if state == "failed":
            return pipeline, "failed"
    
    # Before using stored_phase, validate it's consistent with stage states
    # Don't allow postprocess if rip is not completed (for both hit and miss)
    if stored_phase == "postprocess" and rip_state not in ("completed", "skipped"):
        # Rip is not completed, so phase should be "rip" or "label" (if applicable)
        if profile == "miss" and label_state in ("completed", "skipped"):
            # This shouldn't happen, but if it does, go back to label phase
            return pipeline, "label"
        return pipeline, "rip"
    
    # For miss profile: don't allow postprocess if label is not completed
    if profile == "miss" and stored_phase == "postprocess":
        if label_state not in ("completed", "skipped"):
            return pipeline, "label"
    
    # Don't allow label/postprocess/transfer if rip is not completed
    if stored_phase in ("label", "postprocess", "transfer", "finalize_release") and rip_state not in ("completed", "skipped"):
        return pipeline, "rip"

    if all(state in ("completed", "skipped") for state in pipeline.values()):
        return pipeline, "complete"

    if stored_phase in stage_order or stored_phase in ("complete", "failed"):
        return pipeline, stored_phase

    return pipeline, "rip"


def _job_base_dir(job) -> Optional[Path]:
    """Return the raw directory for a job, or None if it doesn't exist on disk."""
    from core.job_paths import JobPaths
    raw = JobPaths.for_id(str(job.id)).raw
    return raw if raw.exists() else None


def _download_cover(url: str, base_dir: Path, filename: str) -> str | None:
    """
    Download a cover image to base_dir/filename. Returns the relative filename if successful.
    """
    if not url or not url.lower().startswith(("http://", "https://")):
        return None
    try:
        base_dir.mkdir(parents=True, exist_ok=True)
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        target = base_dir / filename
        with open(target, "wb") as fh:
            fh.write(resp.content)
        return filename
    except Exception as exc:
        log.warning("Cover download failed for %s: %s", url, exc)
        return None


def _validate_all_titles_labeled(disc, db: Session) -> tuple[bool, list[str]]:
    """
    Validate that all titles for a disc are labeled according to their type.
    Returns (is_valid, unlabeled_title_ids).
    
    Validation rules:
    - If type is null: validation fails — except for a sole ``active=True`` primary in a
      multi-member segment_map group whose other members are all ``type='ignore'``; that
      row is accepted because ``core.duplicate_group_sync.apply_primary_duplicate_row``
      will/did fill its type with ``'ignore'`` from sibling consensus. This handles
      pre-fix discs where sync demoted secondaries but never set the primary's type.
    - If type == "ignore": no fields required
    - If type == "episode": requires season, episode, and title (name) to be non-empty
    - If type is anything else: requires title (name) to be non-empty

    When a duplicate segment_map group has exactly one ``active=True`` primary, other
    members are skipped: the Ripper UI only validates the primary (``areLabelTitlesComplete``),
    and rows may still carry stale types or cleared names from ``sync_duplicate_group_labels``.
    """
    from core.duplicate_info import _normalize_segment_map

    all_titles = db.query(db_models.DiscTitle).filter(
        db_models.DiscTitle.disc_id == disc.id
    ).all()

    by_seg: dict[str, list] = {}
    for t in all_titles:
        k = _normalize_segment_map(t.segment_map)
        if k is None:
            continue
        by_seg.setdefault(k, []).append(t)
    multi_seg = {k for k, members in by_seg.items() if len(members) > 1}

    def _is_non_primary_duplicate_member(t: db_models.DiscTitle) -> bool:
        k = _normalize_segment_map(t.segment_map)
        if k is None or k not in multi_seg:
            return False
        members = by_seg[k]
        primaries = [m for m in members if m.active is True]
        if len(primaries) != 1:
            return False
        return str(t.id) != str(primaries[0].id)

    def _is_sole_active_primary_with_all_ignore_siblings(t: db_models.DiscTitle) -> bool:
        # Mirrors core.duplicate_group_sync.apply_primary_duplicate_row's NULL-fill rule:
        # when sync would have set this primary's type to 'ignore' from sibling consensus,
        # treat the row as effectively ignored even before sync persists the type.
        k = _normalize_segment_map(t.segment_map)
        if k is None or k not in multi_seg:
            return False
        members = by_seg[k]
        primaries = [m for m in members if m.active is True]
        if len(primaries) != 1 or str(primaries[0].id) != str(t.id):
            return False
        siblings = [m for m in members if str(m.id) != str(t.id)]
        return bool(siblings) and all(
            (m.type or "").strip().lower() == "ignore" for m in siblings
        )

    unlabeled = []
    for title in all_titles:
        if _is_non_primary_duplicate_member(title):
            continue
        title_type = title.type

        # If type is null, validation fails — except for a sole active primary in a
        # multi-member duplicate group whose other members are all 'ignore'. Sync
        # will/did fill that with 'ignore' from sibling consensus.
        if title_type is None:
            if _is_sole_active_primary_with_all_ignore_siblings(title):
                continue
            unlabeled.append(str(title.id))
            continue
        
        # Normalize type to lowercase for comparison
        title_type_lower = title_type.lower() if title_type else None
        
        # If type is "ignore", no fields required
        if title_type_lower == "ignore":
            continue
        
        # If type is "episode", require season, episode, and title
        if title_type_lower == "episode":
            has_season = title.season is not None
            has_episode = title.episode is not None
            has_name = bool(title.title and title.title.strip())
            
            if not (has_season and has_episode and has_name):
                unlabeled.append(str(title.id))
            continue
        
        # For all other types, require title (name) to be non-empty
        has_name = bool(title.title and title.title.strip())
        if not has_name:
            unlabeled.append(str(title.id))
    
    if unlabeled:
        return False, unlabeled

    # #318: Validate that titles with identical names are distinguishable by at least
    # one of: edition, season, episode, or type. Prevents ambiguous output filenames.
    non_ignore = [
        t
        for t in all_titles
        if t.type and t.type.lower() != "ignore" and not _is_non_primary_duplicate_member(t)
    ]
    name_groups: dict[str, list] = {}
    for t in non_ignore:
        key = (t.title or "").strip().lower()
        if key:
            name_groups.setdefault(key, []).append(t)
    ambiguous: list[str] = []
    for name_key, titles_group in name_groups.items():
        if len(titles_group) < 2:
            continue
        # Check each pair within the group for distinguishability
        seen_identity: set[tuple] = set()
        for t in titles_group:
            identity = (
                (t.edition or "").strip().lower(),
                t.season,
                t.episode,
                (t.type or "").strip().lower(),
            )
            if identity in seen_identity:
                ambiguous.append(str(t.id))
            else:
                seen_identity.add(identity)
    if ambiguous:
        return False, ambiguous

    return True, []


def _reapply_label_draft_link_hints_from_lp(disc, lp: Dict[str, Any]) -> None:
    """
    Reconcile disc.label_draft link keys with the label payload (same rules as save_job_workflow_context).

    Used after sync_disc_label_draft_with_release(disc, None) so draft boxset/movie hints from the
    merged labelForm are not lost when the user unlinks the disc but keeps boxset/movie selection.
    """
    if not isinstance(disc.label_draft, dict):
        disc.label_draft = {}
    draft = dict(disc.label_draft)
    if "group_type" in lp:
        v = lp.get("group_type")
        draft["group_type"] = (v or "movie").lower() if v else None
    if "movie_id" in lp:
        draft["movie_id"] = lp["movie_id"]
    if "release_id" in lp:
        draft["release_id"] = lp["release_id"]
    if "boxset_id" in lp:
        draft["boxset_id"] = lp["boxset_id"]
    allowed_label_draft_keys = {"movie_id", "group_type", "release_id", "boxset_id"}
    disc.label_draft = {k: v for k, v in draft.items() if k in allowed_label_draft_keys}
    from sqlalchemy.orm.attributes import flag_modified

    flag_modified(disc, "label_draft")


def _strip_stale_release_hints_if_movie_cleared(lp: Dict[str, Any]) -> None:
    """
    When movie_id is explicitly cleared, merged PATCH bodies may still carry release_id/boxset_id.
    Null those keys so label_draft persistence and _apply_label_to_records cannot re-link the disc.
    """
    if "movie_id" not in lp:
        return
    if lp.get("movie_id") is not None and lp.get("movie_id") != "":
        return
    lp["release_id"] = None
    lp["boxset_id"] = None


def _apply_label_to_records(disc, lp: Dict[str, Any], db: Session) -> None:
    """Persist label data onto release/disc/track records (record-first)."""
    rel = getattr(disc, "release", None)
    if rel and getattr(rel, "finalized", False):
        raise HTTPException(400, detail="Release is finalized and cannot be modified")
    if getattr(disc, "finalized", False):
        raise HTTPException(400, detail="Disc is finalized and cannot be modified")

    _strip_stale_release_hints_if_movie_cleared(lp)

    # Unlink movie: unlink disc from release, delete orphaned release, then delete orphaned boxset if any
    if "movie_id" in lp and (lp.get("movie_id") is None or lp.get("movie_id") == ""):
        old_release_id = disc.release_id
        disc.release_id = None
        # Sync label_draft to clear release fields
        from api.crud import sync_disc_label_draft_with_release
        sync_disc_label_draft_with_release(disc, None)
        rel = None
        db.flush()
        if old_release_id:
            old_release = (
                db.query(db_models.Release)
                .filter(db_models.Release.id == old_release_id)
                .first()
            )
            if old_release:
                from api.crud import cleanup_orphaned_release, cleanup_orphaned_boxset
                old_boxset_id = getattr(old_release, "boxset_id", None)
                cleanup_orphaned_release(db, old_release)
                if old_boxset_id:
                    old_boxset = (
                        db.query(db_models.Boxset)
                        .filter(db_models.Boxset.id == old_boxset_id)
                        .first()
                    )
                    if old_boxset:
                        cleanup_orphaned_boxset(db, old_boxset)

    # Link release only when user explicitly selected one (release_id in payload).
    # Never auto-create a release from movie_id/boxset_id; use Create New Release or Create Boxset endpoints.
    # (A) Explicit clear: user clicked "Change" and cleared the release — unlink disc and delete orphan release.
    # When disc was never linked (release_id already NULL), a merged labelForm still carries release_id: null
    # from GET workflow-context; do not call sync_disc_label_draft_with_release(disc, None) or we wipe
    # label_draft boxset_id / other hints the save path just persisted.
    if "release_id" in lp and (lp.get("release_id") is None or lp.get("release_id") == ""):
        old_release_id = disc.release_id
        disc.release_id = None
        rel = None
        if old_release_id:
            from api.crud import sync_disc_label_draft_with_release

            sync_disc_label_draft_with_release(disc, None)
            _reapply_label_draft_link_hints_from_lp(disc, lp)
        db.flush()  # So cleanup_orphaned_release sees the disc as unlinked
        if old_release_id:
            old_release = (
                db.query(db_models.Release)
                .filter(db_models.Release.id == old_release_id)
                .first()
            )
            if old_release:
                from api.crud import cleanup_orphaned_release

                cleanup_orphaned_release(db, old_release)
    elif lp.get("release_id"):
        # (B) Assign (or reassign) release: link disc to selected release; cleanup previous if orphaned.
        release_id = lp.get("release_id")
        old_release_id = disc.release_id
        existing = (
            db.query(db_models.Release)
            .filter(db_models.Release.id == release_id)
            .first()
        )
        if existing and not getattr(existing, "finalized", False):
            rel = existing
            disc.release_id = rel.id
            # Sync label_draft with release assignment
            from api.crud import sync_disc_label_draft_with_release
            sync_disc_label_draft_with_release(disc, rel)
            db.flush()  # So cleanup_orphaned_release sees the disc as linked to new release
            if old_release_id and str(old_release_id) != str(rel.id):
                old_release = (
                    db.query(db_models.Release)
                    .filter(db_models.Release.id == old_release_id)
                    .first()
                )
                if old_release:
                    from api.crud import cleanup_orphaned_release
                    cleanup_orphaned_release(db, old_release)
        elif existing and getattr(existing, "finalized", False):
            raise HTTPException(400, detail="Release is finalized and cannot be modified")
        # If release_id not found, leave disc.release_id and rel unchanged
    elif not rel and not disc.release_id:
        # No release_id in payload and disc has no release: do not create, do not assign
        pass

    if rel:
        rel.type = lp.get("group_type") or rel.type
        incoming = lp.get("release_name")
        slug_hint = lp.get("release_slug") or getattr(rel, "slug", None)
        if (
            incoming is not None
            and slug_hint is not None
            and str(incoming).strip() == str(slug_hint).strip()
        ):
            incoming = getattr(rel, "name", None)
        rel.name = incoming  # Can be None/blank for edition name
        rel.title = rel.name or rel.title
        # Release slug is auto-generated, ignore if provided
        # rel.slug = lp.get("release_slug") or rel.slug
        # Auto-generate slug if needed
        if not rel.slug or rel.slug.startswith("pending-"):
            from api.routers.releases import _compute_release_slug
            desired_slug = _compute_release_slug(rel, {
                "release_year": lp.get("release_year") or getattr(rel, "release_year", None),
                "release_name": rel.name or rel.title,
            })
            if desired_slug:
                rel.slug = desired_slug
        # tmdb_id removed from Release - use Movie.tmdb_id instead
        rel.upc = lp.get("upc") or rel.upc
        rel.asin = lp.get("asin") or rel.asin
        rel.cover_front_url = lp.get("cover_front_url") or rel.cover_front_url
        rel.cover_back_url = lp.get("cover_back_url") or rel.cover_back_url
        if lp.get("release_year") is not None:
            rel.release_year = lp.get("release_year")
        
        # Update boxset_id if provided; when clearing, cleanup orphaned boxset if it has no other releases
        if "boxset_id" in lp:
            old_boxset_id = getattr(rel, "boxset_id", None)
            rel.boxset_id = lp.get("boxset_id") if lp.get("boxset_id") else None
            if old_boxset_id and not rel.boxset_id:
                old_boxset = (
                    db.query(db_models.Boxset)
                    .filter(db_models.Boxset.id == old_boxset_id)
                    .first()
                )
                if old_boxset:
                    from api.crud import cleanup_orphaned_boxset
                    cleanup_orphaned_boxset(db, old_boxset)
        
        # Update release.movie_id: movie_id / film_id win over tmdb_id when present (stale labelForm.tmdb_id
        # must not override the chosen movie). Use tmdb_id only for DiscDB/TMDB bootstrap when no UUID yet.
        resolved_movie_id: str | None = None
        mid = lp.get("movie_id")
        if mid is not None and str(mid).strip() != "":
            resolved_movie_id = str(mid).strip()
        else:
            fid = lp.get("film_id")
            if fid is not None and str(fid).strip() != "":
                resolved_movie_id = str(fid).strip()
        if not resolved_movie_id and lp.get("tmdb_id"):
            from api.crud import _ensure_movie_from_discdb
            resolved_movie_id = _ensure_movie_from_discdb(db, lp)
        if resolved_movie_id:
            movie = db.query(db_models.Movie).filter(db_models.Movie.id == resolved_movie_id).first()
            if not movie:
                raise HTTPException(404, detail=f"Movie with id {resolved_movie_id} not found")
            rel.movie_id = resolved_movie_id

    disc.format = lp.get("disc_format") or disc.format
    
    from api.crud import apply_disc_slug_from_label_payload, _title_case, default_disc_name

    payload_info = lp.get("info_title") or _title_case(lp.get("info_label"))
    effective_info_title = payload_info or disc.info_title
    disc_format_eff = disc.format or lp.get("disc_format")

    # Auto-populate disc_name from info title + format when not set (user can still edit it)
    disc_name = lp.get("disc_name")
    if not disc_name and not disc.disc_name:
        auto_name = default_disc_name(disc_format_eff, effective_info_title)
        if auto_name:
            disc_name = auto_name
            disc.disc_name = disc_name
    elif disc_name:
        # User provided disc_name, use it
        disc.disc_name = disc_name

    apply_disc_slug_from_label_payload(disc, lp.get("disc_slug"))
    if rel:
        # Backend is source of truth for disc numbers on label save.
        # Only assign if missing to avoid overwriting normalized values.
        if disc.disc_number is None:
            from api.crud import normalize_disc_numbers_for_release
            disc_number_map = normalize_disc_numbers_for_release(db, rel)
            disc.disc_number = disc_number_map.get(disc.id, disc.disc_number)
    elif lp.get("disc_number") is not None:
        disc.disc_number = lp.get("disc_number")

    # labelForm from workflow uses "tracks"; some callers use "titles" (same shape).
    titles_payload = lp.get("titles") or lp.get("tracks") or []
    if titles_payload:
        # Get existing titles indexed by id (title_id) for efficient lookup
        existing_titles = {
            title.id: title
            for title in db.query(db_models.DiscTitle)  # type: ignore[attr-defined]
            .filter(db_models.DiscTitle.disc_id == disc.id)  # type: ignore
            .all()
        }
        
        payload_by_title_id: dict[str, dict[str, Any]] = {}
        seen: set[str] = set()
        updated_titles = []
        
        for idx, t in enumerate(titles_payload):
            # title_id is the unique identifier (maps to DiscTitle.id)
            title_id = t.get("title_id")
            if not title_id:
                import logging
                logger = logging.getLogger(__name__)
                logger.warning(f"Missing title_id for title at index {idx}: {t.get('title', 'unknown')}")
                continue  # Skip this title if title_id is missing
            title_id_str = str(title_id)
            if title_id_str in seen:
                continue  # de-duplicate incoming payload
            seen.add(title_id_str)
            payload_by_title_id[title_id_str] = t
            
            # Find or create title
            existing_title = existing_titles.get(title_id_str)
            if existing_title:
                # Update existing title - only user-editable fields
                # workflow-context save is user-initiated; route every
                # label field through the source-aware helper so user_*
                # owns the values + the resolved cache stays synced.
                from api.crud import set_title_field
                type_value = _normalize_title_type(t.get("type"))
                set_title_field(existing_title, "type", type_value, source="user")
                set_title_field(existing_title, "title", t.get("title") or t.get("episode_name"), source="user")
                set_title_field(existing_title, "description", t.get("description") or t.get("note"), source="user")
                set_title_field(existing_title, "season", t.get("season"), source="user")
                set_title_field(existing_title, "episode", t.get("episode"), source="user")
                existing_title.comment = t.get("comment") or t.get("output_file") or existing_title.comment
                existing_title.order_index = idx
                
                # Mark as modified to ensure SQLAlchemy tracks the change
                from sqlalchemy.orm.attributes import flag_modified
                flag_modified(existing_title, 'type')
                flag_modified(existing_title, 'title')
                flag_modified(existing_title, 'description')
                updated_titles.append(existing_title)
            else:
                # Title not found by title_id in existing_titles
                # Check if title already exists by title_id (might have been created in a previous transaction)
                existing_title = db.query(db_models.DiscTitle).filter(  # type: ignore[attr-defined]
                    db_models.DiscTitle.id == title_id_str  # type: ignore
                ).first()
                
                # If still not found, check by (disc_id, source_file) to handle cases where
                # the same source_file exists but with a different title_id
                if not existing_title:
                    source_file = t.get("source_file") or t.get("output_file") or t.get("track_id") or str(title_id_str)
                    existing_title = (
                        db.query(db_models.DiscTitle)  # type: ignore[attr-defined]
                        .filter(
                            db_models.DiscTitle.disc_id == disc.id,  # type: ignore
                            db_models.DiscTitle.source_file == source_file,  # type: ignore
                        )
                        .order_by(db_models.DiscTitle.index.asc().nulls_last())  # type: ignore
                        .first()
                    )
                
                if existing_title:
                    # Update existing title instead of creating new one
                    source_file = t.get("source_file") or t.get("output_file") or t.get("track_id") or str(title_id_str)
                    seg_raw = t.get("segment_map")
                    if isinstance(seg_raw, list):
                        segment_map = ",".join([str(x) for x in seg_raw])
                    else:
                        segment_map = seg_raw
                    
                    update_fields = {
                        "disc_id": disc.id,
                        "index": t.get("index", idx),
                        "comment": t.get("comment") or t.get("output_file"),
                        "source_file": source_file,
                        "segment_map": segment_map,
                        "duration": t.get("duration"),
                        "duration_raw": t.get("duration_raw"),
                        "size": t.get("size"),
                        "display_size": t.get("display_size"),
                        "description": t.get("description") or t.get("note"),
                        "title": t.get("title") or t.get("episode_name"),
                        "type": _normalize_title_type(t.get("type")),
                        "season": t.get("season"),
                        "episode": t.get("episode"),
                        "chapters": t.get("chapters"),
                        "streams": t.get("streams"),
                        "content": t.get("content", True),
                        "order_index": idx,
                        "language_code": t.get("language_code"),
                        "language": t.get("language"),
                    }
                    
                    for k, v in update_fields.items():
                        if hasattr(existing_title, k):
                            setattr(existing_title, k, v)
                    updated_titles.append(existing_title)
                else:
                    # Create new title only if it doesn't exist
                    source_file = t.get("source_file") or t.get("output_file") or t.get("track_id") or str(title_id_str)
                    seg_raw = t.get("segment_map")
                    if isinstance(seg_raw, list):
                        segment_map = ",".join([str(x) for x in seg_raw])
                    else:
                        segment_map = seg_raw
                    new_title = db_models.DiscTitle(  # type: ignore[attr-defined]
                        id=title_id_str,  # Use the provided title_id as the id
                        disc_id=disc.id,
                        index=t.get("index", idx),
                        comment=t.get("comment") or t.get("output_file"),
                        source_file=source_file,
                        segment_map=segment_map,
                        duration=t.get("duration"),
                        duration_raw=t.get("duration_raw"),
                        size=t.get("size"),
                        display_size=t.get("display_size"),
                        description=t.get("description") or t.get("note"),
                        title=t.get("title") or t.get("episode_name"),
                        type=_normalize_title_type(t.get("type")),
                        season=t.get("season"),
                        episode=t.get("episode"),
                        chapters=t.get("chapters"),
                        streams=t.get("streams"),
                        content=t.get("content", True),
                        order_index=idx,
                        language_code=t.get("language_code"),
                        language=t.get("language"),
                    )
                    db.add(new_title)
                    updated_titles.append(new_title)

        # Flush changes to database before commit (ensures SQLAlchemy writes pending changes)
        db.flush()
        
        # Note: We do NOT delete/recreate tracks when updating title metadata
        # Tracks are separate entities that should only be modified when streams data changes
        # This function only updates title metadata (type, title, description, etc.), not streams


def _sync_job_disc_payload_disc_label_fields(job: db_models.Job, disc: db_models.Disc) -> None:
    """Mirror disc row onto job.disc_payload and nested label_payload so GET workflow-context does not read stale copies."""
    from sqlalchemy.orm.attributes import flag_modified

    payload = dict(job.disc_payload or {})
    payload["disc_name"] = disc.disc_name
    payload["disc_slug"] = disc.disc_slug
    payload["disc_number"] = disc.disc_number
    fmt = getattr(disc, "format", None)
    if fmt is not None:
        payload["disc_format"] = fmt
    inner = dict(payload.get("label_payload") or {})
    inner["disc_name"] = disc.disc_name
    inner["disc_slug"] = disc.disc_slug
    inner["disc_number"] = disc.disc_number
    if fmt is not None:
        inner["disc_format"] = fmt
    payload["label_payload"] = inner
    job.disc_payload = payload
    flag_modified(job, "disc_payload")


def _default_workflow_step(job) -> str:
    from core.job_state import _infer_profile
    profile = _infer_profile(job)
    return "summary" if profile == "hit" else "film"


def _derive_per_title_status(per_title_progress, disc_payload):
    """Derive perTitleStatus (title_id -> 'completed'|'running'|'pending'|'skipped') from per_title_progress."""
    if not per_title_progress:
        return None
    completed_titles = (disc_payload or {}).get("completed_titles")
    skipped_titles = (disc_payload or {}).get("skipped_titles")
    out = {}
    for k, v in per_title_progress.items():
        try:
            pct = int(v)
        except Exception:
            pct = 0
        if skipped_titles and k in skipped_titles:
            out[k] = "skipped"
        elif pct >= 100:
            out[k] = "completed"
        elif pct > 0:
            out[k] = "running"
        else:
            out[k] = "pending"
    if completed_titles:
        for k in completed_titles:
            out[k] = "completed"
    if skipped_titles:
        for k in skipped_titles:
            out[k] = "skipped"
    return out


def _normalize_transfer_paths(value: Any) -> Optional[List[str]]:
    """Ensure transfer_paths is None or a list of strings (DB JSON can store nulls)."""
    if value is None:
        return None
    if not isinstance(value, list):
        return None
    return [str(x) if x is not None else "" for x in value]


def _build_job_status(job, job_created: bool | None = None) -> JobStatus:
    disc = getattr(job, "disc", None)
    rel = getattr(disc, "release", None) if disc else None
    disc_payload_raw = getattr(job, "disc_payload", None) or {}
    # Strip transient label fields and heavy internal processing data from disc_payload.
    # These fields are used only by backend workers (rip/postprocess) and are never displayed
    # by the frontend. Stripping them reduces disc_payload from ~132KB to ~5KB per job.
    _STRIP_DISC_PAYLOAD_KEYS = frozenset({
        "label_payload", "label_draft",
        # Heavy processing artifacts (65KB+ combined):
        "metadata_results", "detection_results",
        # Note: "previews" is NOT stripped — frontend reads disc_payload.previews
        # for preview track counts in progress update handlers.
        "source_hashes", "source_files", "output_files",
        "title_output_map", "title_filename_map",
        "discovered_titles", "completed_titles",
        # Already exposed as top-level jobStatus fields:
        "ripped_files", "post_paths",
        # Raw scan data (stored in disc.disc_info, not needed in jobStatus):
        "raw_info_log", "info_log", "makemkv_info_log",
        "titles_map", "scan_tracks", "cinfo_lines",
    })
    disc_payload = {k: v for k, v in disc_payload_raw.items() if k not in _STRIP_DISC_PAYLOAD_KEYS}
    preview = None
    # Extract preview from raw payload before stripping (needed for PreviewInfo)
    preview_raw = disc_payload_raw.get("previews")
    if preview_raw:
        try:
            preview = PreviewInfo(**preview_raw)
        except Exception:
            preview = None
    # Default: labeling not required when DiscDB hit
    label_required = bool(disc_payload.get("label_required"))
    label_ready = bool(disc_payload.get("label_ready"))
    if not label_required:
        label_ready = True if disc_payload.get("label_ready") is None else label_ready
    disc_hash = disc.content_hash if disc else disc_payload.get("disc_hash")
    # Get boxset_id if release is linked to a boxset
    boxset_id = getattr(rel, "boxset_id", None) if rel else None
    
    # Get label_draft early to check if it has a different movie_id (needed for production_year and later use)
    stored_label_draft = None
    if disc and hasattr(disc, "label_draft") and disc.label_draft:
        stored_label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else None
    if not stored_label_draft:
        stored_label_draft = disc_payload_raw.get("label_draft")
    
    # Get production_year from movie, release_year from boxset or release
    production_year = None
    release_year = None
    resolution = None
    if rel:
        if rel.movie:
            production_year = rel.movie.production_year
        # Get release_year from boxset if available, otherwise from release
        # Check boxset_id first, then try to access boxset (may not be loaded)
        if hasattr(rel, "boxset_id") and rel.boxset_id:
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
    
    payload = {
        "disc_hash": disc_hash,
        "disc_slug": disc.disc_slug if disc else None,
        "disc_name": disc.disc_name if disc else None,
        "disc_number": disc.disc_number if disc else None,
        "disc_format": disc.format if disc else None,
        "disc_id": str(disc.id) if disc else None,
        "disc_group": rel.slug if rel else None,
        "group_type": rel.type if rel else None,
        "release_name": rel.name if rel else None,
        "release_slug": rel.slug if rel else None,
        "release_id": str(rel.id) if rel else None,
        "movie_name": rel.movie.name if rel and rel.movie else None,
        "boxset_id": boxset_id,
        "tmdb_id": rel.movie.tmdb_id if rel and rel.movie else None,
        "upc": rel.upc if rel else None,
        "asin": rel.asin if rel else None,
        "cover_front_url": rel.cover_front_url if rel else None,
        "cover_back_url": rel.cover_back_url if rel else None,
    }
    # label_draft holds only movie_id and group_type; do not add boxset_id or release_* to it

    pipeline, phase = _derive_pipeline(job)
    workflow_step = getattr(job, "workflow_step", None) or (
        stored_label_draft.get("workflow_step") if isinstance(stored_label_draft, dict) else None
    ) or _default_workflow_step(job)
    return JobStatus(
        jobId=str(job.id),
        disc_id=payload.get("disc_id"),
        release_id=payload.get("release_id"),
        movie_name=payload.get("movie_name"),
        boxset_id=boxset_id,
        release_year=release_year,
        production_year=production_year,
        resolution=resolution,
        job_status=job.job_status,
        scan_state=getattr(job, "scan_state", None),
        rip_progress=job.rip_progress,
        rip_phase=getattr(job, "rip_phase", None),
        post_progress=getattr(job, "post_progress", 0),
        logs=job.logs or [],
        job_dir=str(JobPaths.for_id(str(job.id)).root),
        ripped_files=getattr(job, "ripped_files", None),
        post_paths=getattr(job, "post_paths", None),
        artifacts=getattr(disc, "artifacts", None) if disc else None,
        error_reason=getattr(job, "error_reason", None),
        transfer_paths=_normalize_transfer_paths(getattr(job, "transfer_paths", None)),
        transfer_error=getattr(job, "transfer_error", None),
        transfer_progress=getattr(job, "transfer_progress", None),
        transfer_verification_hash=getattr(job, "transfer_verification_hash", None),
        transfer_verification_status=getattr(job, "transfer_verification_status", None),
        transfer_retry_count=getattr(job, "transfer_retry_count", None),
        transfer_max_retries=getattr(job, "transfer_max_retries", None),
        transfer_speed_mbps=getattr(job, "transfer_speed_mbps", None),
        transfer_bytes_transferred=getattr(job, "transfer_bytes_transferred", None),
        transfer_total_bytes=getattr(job, "transfer_total_bytes", None),
        transfer_conflict_resolution=getattr(job, "transfer_conflict_resolution", None),
        transfer_source_cleaned=getattr(job, "transfer_source_cleaned", None),
        transfer_validation_status=getattr(job, "transfer_validation_status", None),
        transfer_validation_error=getattr(job, "transfer_validation_error", None),
        transfer_deduplicated=getattr(job, "transfer_deduplicated", None),
        stage_profile=getattr(job, "stage_profile", None),
        discdb_result=getattr(job, "discdb_result", None),
        rip_state=getattr(job, "rip_state", None),
        rip_started_at=getattr(job, "rip_started_at", None),
        rip_completed_at=getattr(job, "rip_completed_at", None),
        label_state=getattr(job, "label_state", None) or pipeline.get("label"),
        finalize_state=getattr(job, "finalize_state", None),
        post_state=job.derived_post_state,  # #365 — derived, not column
        transfer_state=getattr(job, "transfer_state", None),
        transfer_phase=getattr(job, "transfer_phase", None),
        finalize_release_state=getattr(job, "finalize_release_state", None),
        titlesCompleted=getattr(job, "titles_completed", None),
        totalTitles=getattr(job, "total_titles", None),
        currentTitleProgress=getattr(job, "current_title_progress", None),
        currentTitleId=getattr(job, "current_title_id", None),
        currentTitleNumber=getattr(job, "current_title_number", None),
        perTitleProgress=getattr(job, "per_title_progress", None),
        perTitleStatus=_derive_per_title_status(getattr(job, "per_title_progress", None), disc_payload_raw),
        disc_hash=payload.get("disc_hash"),
        disc_group=payload.get("disc_group"),
        group_type=payload.get("group_type"),
        disc_payload=disc_payload or None,
        label_draft=stored_label_draft,
        label_required=label_required,
        label_ready=label_ready,
        preview=preview,
        pipeline=pipeline,
        phase=phase,
        dev_mode=getattr(job, "dev_mode", None),
        dev_validation=getattr(job, "dev_validation", None),
        export_path=getattr(job, "export_path", None),
        workflow_step=workflow_step,
        job_created=job_created,
        segment_reorder_state=getattr(job, "segment_reorder_state", None),
        rip_set=getattr(job, "rip_set", None),
    )


def _cleanup_stale_jobs(
    db: Session,
    disc_hash: Optional[str] = None,
    mount_point: Optional[str] = None,
) -> list[str]:
    """
    Fail jobs that have been 'pending' or 'running' for too long without any progress.
    This prevents the UI from thinking an abandoned rip is still active.
    
    Also detects jobs stuck in 'running' state with post_state='running' but no active task
    (e.g., after a revert where task enqueue failed).
    
    Also checks if Celery tasks are still running - if a task was killed (e.g., service restart),
    the job will be marked as failed.
    
    When disc_hash or mount_point is provided (e.g. from start_rip), jobs for that disc that are
    being failed as orphaned use a "superseded" error reason so we don't emit job_finished/context_changed
    (avoids overwriting the new job's context when the user retries after restart).
    """
    if STALE_JOB_TIMEOUT_SECONDS <= 0:
        return []

    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=STALE_JOB_TIMEOUT_SECONDS)
    
    # NEW: Check for jobs with celery_task_id where the task is no longer running
    # This catches cases where the service was restarted and tasks were killed
    # Also check failed jobs to ensure they're properly marked if task is terminated
    try:
        jobs_with_tasks = (
            db.query(crud.models.Job)  # type: ignore[attr-defined]
            .filter(
                or_(
                    crud.models.Job.job_status.in_(["pending", "running"]),  # type: ignore[attr-defined]
                    crud.models.Job.rip_state == "failed",  # type: ignore[attr-defined]
                ),
                crud.models.Job.celery_task_id.isnot(None),  # type: ignore[attr-defined]
            )
            .all()
        )
    except ProgrammingError as e:
        # Schema not ready (e.g. migrations not run, wrong DB) - skip cleanup
        orig = getattr(e, "orig", None)
        if orig is not None and getattr(orig, "pgcode", None) == "42P01":  # undefined_table
            log.warning("_cleanup_stale_jobs: jobs table not available (schema not ready?): %s", e)
            return
        raise

    orphaned_jobs = []

    inspect_replied, worker_task_ids = _collect_celery_tasks_on_workers()

    for job in jobs_with_tasks:
        try:
            from workers.tasks import celery_app
            from celery.result import AsyncResult

            task_result = AsyncResult(job.celery_task_id, app=celery_app)
            task_state = task_result.state

            if job.celery_task_id in worker_task_ids:
                log.debug(
                    "Job %s: Celery task %s is on a worker (active/reserved/scheduled), skipping orphan celery check",
                    job.id,
                    job.celery_task_id,
                )
                continue

            rip_pid = getattr(job, "rip_pid", None)
            if rip_pid is not None and is_pid_alive(rip_pid):
                log.debug(
                    "Job %s: rip_pid %s alive, skipping orphan celery check",
                    job.id,
                    rip_pid,
                )
                continue

            if getattr(job, "rip_state", None) == "running" and _rip_operation_lock_held_for_job(job):
                log.info(
                    "Job %s: disc rip lock held (OPERATION_RIP) — skipping Celery orphan mark "
                    "(worker likely mid-rip; inspect missed task id)",
                    job.id,
                )
                continue

            if not inspect_replied:
                continue

            if task_state == "SUCCESS":
                ctid = str(job.celery_task_id or "")
                if (
                    getattr(job, "rip_state", None) == "running"
                    and getattr(job, "rip_phase", None) == "verification"
                    and ctid.startswith("rip_disc:")
                ):
                    log.info(
                        "Job %s: rip_disc SUCCESS but rip still running in verification — enqueue rip_verification heal",
                        job.id,
                    )
                    try:
                        enqueue_rip_verification_for_job(str(job.id), reason="orphan rip_disc success heal")
                    except Exception as heal_exc:
                        log.warning("rip_verification heal failed for job %s: %s", job.id, heal_exc)
                else:
                    log.info(
                        "Job %s: Celery task %s completed successfully (state: SUCCESS) but job status is '%s' - skipping orphan mark",
                        job.id,
                        job.celery_task_id,
                        job.job_status,
                    )
                continue

            if task_state in ("PENDING", "STARTED", "PROGRESS"):
                if getattr(job, "rip_state", None) != "running":
                    continue
                if _rip_orphan_inspect_within_grace(job, task_state):
                    log.info(
                        "Job %s: Celery task %s state=%s not on workers but job updated within "
                        "RIP_ORPHAN_INSPECT_GRACE_SECONDS=%s — skipping orphan mark",
                        job.id,
                        job.celery_task_id,
                        task_state,
                        RIP_ORPHAN_INSPECT_GRACE_SECONDS,
                    )
                    continue
                if job.job_status != "failed" or job.rip_state != "failed":
                    log.warning(
                        "Job %s: Celery task %s result state=%s but task not on workers and rip_state=running - marking as orphaned",
                        job.id,
                        job.celery_task_id,
                        task_state,
                    )
                    orphaned_jobs.append((job, task_state))
                continue

            if job.rip_state == "failed" and job.job_status != "failed":
                log.warning(
                    "Job %s: Celery task %s is in state '%s' and rip_state is 'failed' but job_status is '%s' - ensuring job is marked as failed",
                    job.id,
                    job.celery_task_id,
                    task_state,
                    job.job_status,
                )
                try:
                    apply_job_state(
                        db,
                        job,
                        updates={"job_status": "failed", "workflow_step": _default_workflow_step(job)},
                        reason=f"Celery task terminated (state: {task_state})",
                    )
                    db.commit()
                    log.info("Job %s: Marked as failed due to terminated Celery task", job.id)
                    if JobPaths.for_id(str(job.id)).root.exists():
                        cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
                    continue
                except Exception as exc:
                    log.error("Job %s: Failed to mark as failed: %s", job.id, exc)
                    db.rollback()

            if getattr(job, "rip_state", None) in ("completed", "skipped"):
                continue

            if job.job_status != "failed" or job.rip_state != "failed":
                log.warning(
                    "Job %s: Celery task %s is in state '%s' but job status is '%s' - marking as orphaned",
                    job.id,
                    job.celery_task_id,
                    task_state,
                    job.job_status,
                )
                orphaned_jobs.append((job, task_state))
        except Exception as exc:
            log.warning("Job %s: Could not check Celery task state for %s: %s", job.id, job.celery_task_id, exc)
    
    # Standard stale job check: pending/running jobs with no progress
    # But first verify with drive manager if job is actually running
    # Not running transfer: transfer_state is NULL or not "running"
    stale_candidates = (
        db.query(crud.models.Job)  # type: ignore[attr-defined]
        .filter(
            crud.models.Job.job_status.in_(["pending", "running"]),  # type: ignore[attr-defined]
            crud.models.Job.updated_at < cutoff,  # type: ignore[attr-defined]
            or_(
                crud.models.Job.transfer_state.is_(None),  # type: ignore[attr-defined]
                crud.models.Job.transfer_state != "running",  # type: ignore[attr-defined]
            ),
            crud.models.Job.rip_progress <= 0,  # type: ignore[attr-defined]
        )
        .all()
    )
    
    # Verify with drive manager state - if job is actually running there, don't mark as stale
    stale = []
    for job in stale_candidates:
        # Only check drive manager for rip jobs (not post-processing or transfer)
        if job.rip_state in ("pending", "running") or job.job_status == "running":
            try:
                drive_state = _get_drive_state()
                if drive_state:
                    operation = drive_state.get_operation_by_job_id(str(job.id))
                    if operation:
                        # Job is actually running in drive manager - not stale
                        log.info("Job %s marked as stale in DB but is active in drive manager - skipping stale cleanup", job.id)
                        continue
            except Exception as exc:
                # Unexpected error - log but still consider stale
                log.warning("Unexpected error verifying job %s with drive manager state: %s - marking as stale", job.id, exc)

        # Hung-rip detection is owned by the worker (RIP_OUTPUT_STALL_SECONDS on makemkv stdout / log tail).
        # Do not fail a rip as "no DB progress" while the subprocess or Celery task is clearly active.
        celery_tid = getattr(job, "celery_task_id", None)
        if celery_tid and celery_tid in worker_task_ids:
            log.info(
                "Job %s stale candidate skipped: celery task id on worker (rip stall owned by worker)",
                job.id,
            )
            continue
        rip_pid = getattr(job, "rip_pid", None)
        if (
            getattr(job, "rip_state", None) == "running"
            and rip_pid is not None
            and is_pid_alive(rip_pid)
        ):
            log.info(
                "Job %s stale candidate skipped: rip_pid %s alive (rip stall owned by worker)",
                job.id,
                rip_pid,
            )
            continue
        try:
            raw_log = JobPaths.for_id(str(job.id)).raw / "makemkv_progress.log"
            if raw_log.is_file():
                mtime = datetime.datetime.fromtimestamp(
                    raw_log.stat().st_mtime, tz=datetime.timezone.utc
                )
                if mtime >= cutoff:
                    log.info(
                        "Job %s stale candidate skipped: makemkv_progress.log updated within stale window",
                        job.id,
                    )
                    continue
        except OSError as ose:
            log.debug("Stale skip log mtime check failed for %s: %s", job.id, ose)

        stale.append(job)
    
    # Health check: Jobs stuck in post-processing state without active task.
    # Catches cases where task enqueue failed or task never started.
    # #365 — translated from `post_state == "running"` to the derivation-
    # equivalent SQL filter `transfer_phase == "preparing"`. Per the
    # decision table in Job.derived_post_state, the only state where the
    # property returns "running" is when ``transfer_phase == "preparing"``
    # (the collapsed-model invariant: preparing IS the postprocess running
    # phase). The two filters select the same jobs for all states the
    # StageState helpers produce. The in-Python re-check at the action
    # dispatch site (migrated in #463) already reads job.derived_post_state.
    STALE_POSTPROCESS_TIMEOUT = 600  # 10 minutes - allow long post-process runs before considering stuck
    postprocess_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=STALE_POSTPROCESS_TIMEOUT)
    stuck_postprocess = (
        db.query(crud.models.Job)  # type: ignore[attr-defined]
        .filter(
            crud.models.Job.job_status == "running",  # type: ignore[attr-defined]
            crud.models.Job.transfer_phase == "preparing",  # type: ignore[attr-defined]
            crud.models.Job.updated_at < postprocess_cutoff,  # type: ignore[attr-defined]
            crud.models.Job.rip_state.in_(["completed", "skipped"]),  # type: ignore[attr-defined]
        )
        .all()
    )
    stuck_postprocess_ids: set[str] = {str(j.id) for j in stuck_postprocess}
    
    # Note: We no longer auto-enqueue post-process after finalize.
    # Jobs with finalize_state=completed and post_state=ready are waiting for manual trigger
    # via POST /jobs/{job_id}/postprocess, so this is expected behavior, not a stuck state.
    
    # Health check: Jobs with stuck preview generation
    # Detect jobs where previews are stuck in "queued" or "running" for too long
    STALE_PREVIEW_TIMEOUT = 300  # 5 minutes - previews can take a while
    preview_cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=STALE_PREVIEW_TIMEOUT)
    stuck_previews = []
    # Query jobs that have finished ripping and might have previews
    jobs_with_previews = (
        db.query(crud.models.Job)  # type: ignore[attr-defined]
        .filter(
            crud.models.Job.rip_state.in_(["completed", "skipped"]),  # type: ignore[attr-defined]
            crud.models.Job.job_status.in_(["running", "completed"]),  # type: ignore[attr-defined]
        )
        .all()
    )
    stuck_preview_ids: set[str] = set()
    for job in jobs_with_previews:
        disc_payload = job.disc_payload or {}
        previews = disc_payload.get("previews", {})
        if isinstance(previews, dict):
            preview_status = previews.get("status")
            updated_at_str = previews.get("updated_at")
            # Check if previews are stuck in queued/running state
            if preview_status in ("queued", "running"):
                is_stuck = False
                if updated_at_str:
                    try:
                        updated_at = datetime.datetime.fromisoformat(updated_at_str.replace("Z", "+00:00"))
                        if updated_at.tzinfo is None:
                            updated_at = updated_at.replace(tzinfo=datetime.timezone.utc)
                        if updated_at < preview_cutoff:
                            is_stuck = True
                    except (ValueError, AttributeError):
                        if job.updated_at < preview_cutoff:
                            is_stuck = True
                else:
                    if job.updated_at < preview_cutoff:
                        is_stuck = True
                if is_stuck:
                    stuck_previews.append(job)
                    stuck_preview_ids.add(str(job.id))
    
    # Combine all sets (avoid duplicates)
    all_stale = {job.id: job for job in stale}
    
    # Add orphaned jobs (tasks that are no longer running)
    orphaned_job_map = {}
    for job, task_state in orphaned_jobs:
        if job.id not in all_stale:
            all_stale[job.id] = job
            orphaned_job_map[job.id] = task_state
            log.warning(
                "Job %s: Celery task %s is not running (state: %s) - marking as failed",
                job.id, job.celery_task_id, task_state
            )
    
    for job in stuck_postprocess:
        if job.id not in all_stale:
            all_stale[job.id] = job
            log.warning(
                "Job %s: Health check detected stuck post-process (running for >%ds, no updates)",
                job.id, STALE_POSTPROCESS_TIMEOUT
            )
    for job in stuck_previews:
        if job.id not in all_stale:
            all_stale[job.id] = job
            log.warning(
                "Job %s: Health check detected stuck preview generation (status=%s for >%ds)",
                job.id, (job.disc_payload or {}).get("previews", {}).get("status") if isinstance(job.disc_payload, dict) else "unknown", STALE_PREVIEW_TIMEOUT
            )
    
    if not all_stale:
        return []

    failed_ids: list[str] = []
    for job in all_stale.values():
        try:
            error_msg = "Job timed out with no progress; marked failed."
            
            # Check if this is an orphaned job (Celery task not running)
            if job.id in orphaned_job_map:
                task_state = orphaned_job_map[job.id]
                error_msg = f"Celery task was terminated (likely due to service restart). Task state: {task_state}."
            
            recovery_attempted = False
            
            # Handle stuck post-process (running but no updates).
            # Bucket-gate against ``stuck_postprocess_ids``: a job in the
            # stuck-preview bucket can transiently have ``post_state="running"``
            # (e.g. the user just clicked Start Postprocess while old preview
            # metadata is stale). Without this gate the preview-stale detection
            # would fire the postprocess-reset action and break an in-flight
            # postprocess by forcing job_status running→pending, which then
            # makes the worker's pending→validating transition raise
            # StateViolation. The two stuck-detection paths must dispatch
            # to their own actions independently.
            if (
                str(job.id) in stuck_postprocess_ids
                # #365 — derived, not column. The SQL filter at line ~1362
                # used the column to build stuck_postprocess_ids; this
                # in-Python check is a defensive re-confirmation. Derivation
                # === column for all states StageState produces (per the
                # truth-table tests in test_derived_post_state.py).
                and job.derived_post_state == "running"
                and job.job_status == "running"
            ):
                error_msg = f"Post-process task appears stuck (no updates for >{STALE_POSTPROCESS_TIMEOUT}s)."
                # Validate post-processing state before recovery
                try:
                    from core.stage_validation import validate_transfer_prep_output
                    paths = JobPaths.from_job(job)
                    validation_result = validate_transfer_prep_output(job, db, paths)
                    if not validation_result.valid:
                        errors_str = "; ".join(validation_result.errors[:3])  # Include first 3 errors
                        error_msg += f" Validation errors: {errors_str}"
                        log.warning("Job %s: Post-processing validation failed: %s", job.id, validation_result.errors)
                except Exception as val_exc:
                    log.warning("Job %s: Post-processing validation error: %s", job.id, val_exc)
                
                # For stuck post-process, revert to pending state instead of failed
                # This allows the user to manually retry or the system to auto-trigger again
                # Use devmode version to allow backward transition (running -> pending)
                if is_dev_mode():
                    pass
                else:
                    # In production, attempt recovery before marking as failed
                    try:
                        from core.failure_recovery import attempt_recovery
                        recovery_successful, recovery_message = attempt_recovery(job, db, error_msg)
                        if not recovery_successful:
                            # Recovery failed - mark as failed via StageState
                            StageState.postprocess_failed(
                                db, job, error_reason=error_msg, reason="stale post-process health check"
                            )
                            apply_job_state(
                                db, job,
                                updates={"workflow_step": _default_workflow_step(job)},
                                reason="workflow_step reset after postprocess failed",
                            )
                            # Emit disc context update after job status change
                            try:
                                import asyncio
                                try:
                                    loop = asyncio.get_running_loop()
                                    asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                                except RuntimeError:
                                    from api.main import _app_instance
                                    if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                        loop = _app_instance.state.event_loop
                                        asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
                            except Exception as exc:
                                log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
                            failed_ids.append(str(job.id))
                            if JobPaths.for_id(str(job.id)).root.exists():
                                cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
                        else:
                            log.info("Job %s: Recovery successful: %s", job.id, recovery_message)
                    except Exception as recovery_exc:
                        log.error("Job %s: Recovery system error: %s", job.id, recovery_exc, exc_info=True)
                        # Fall back to marking as failed via StageState
                        StageState.postprocess_failed(
                            db, job, error_reason=error_msg, reason="stale post-process health check"
                        )
                        apply_job_state(
                            db, job,
                            updates={"workflow_step": _default_workflow_step(job)},
                            reason="workflow_step reset after postprocess failed",
                        )
                        # Emit disc context update after job status change
                        try:
                            import asyncio
                            try:
                                loop = asyncio.get_running_loop()
                                asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                            except RuntimeError:
                                from api.main import _app_instance
                                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                    loop = _app_instance.state.event_loop
                                    asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
                        except Exception as exc:
                            log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
                        failed_ids.append(str(job.id))
                        if JobPaths.for_id(str(job.id)).root.exists():
                            cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
            # Handle stuck preview generation
            elif str(job.id) in stuck_preview_ids:
                log.info("Job %s: Attempting to recover stuck preview generation", job.id)
                if str(job.id) in active_generate_previews_job_ids():
                    log.info(
                        "Job %s: Skipping stuck preview auto-recovery; generate_previews already active on a worker",
                        job.id,
                    )
                else:
                    try:
                        db.refresh(job)
                        disc_payload = job.disc_payload or {}
                        previews_block = disc_payload.get("previews") if isinstance(disc_payload.get("previews"), dict) else {}
                        attempts = int(previews_block.get("auto_recovery_attempts") or 0)

                        is_valid = True
                        val_errors: list[str] = []
                        try:
                            from core.job_validation import validate_previews

                            is_valid, val_errors = validate_previews(job, db)
                        except Exception as val_exc:
                            log.warning("Job %s: Preview validation error: %s", job.id, val_exc)
                            is_valid = False
                            val_errors = [str(val_exc)]

                        if not is_valid:
                            log.warning("Job %s: Preview validation failed before recovery: %s", job.id, val_errors)
                            if attempts >= PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS:
                                msg = "; ".join(val_errors[:3]) if val_errors else "Preview validation failed"
                                tracks = previews_block.get("tracks") if isinstance(previews_block.get("tracks"), dict) else {}
                                disc_payload["previews"] = {
                                    "status": "failed",
                                    "tracks": tracks,
                                    "updated_at": datetime.datetime.utcnow().isoformat(),
                                    "auto_recovery_attempts": attempts,
                                    "auto_recovery_last_error": msg,
                                }
                                job.disc_payload = disc_payload
                                db.commit()
                                log.warning(
                                    "Job %s: Auto preview recovery stopped after %d failed validation(s)",
                                    job.id,
                                    attempts,
                                )
                            else:
                                post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
                                ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
                                file_paths = post_paths if post_paths else ripped_files
                                synthetic_override: Dict[str, Any] | None = None
                                if not file_paths:
                                    existing_tracks = previews_block.get("tracks", {}) if isinstance(previews_block.get("tracks"), dict) else {}
                                    if existing_tracks:
                                        file_paths = {str(k): None for k in existing_tracks.keys()}
                                        synthetic_override = file_paths
                                        log.info(
                                            "Job %s: Using existing preview tracks for recovery (no file_paths on job)",
                                            job.id,
                                        )
                                if not file_paths:
                                    log.warning(
                                        "Job %s: Cannot recover previews - no file paths or existing tracks",
                                        job.id,
                                    )
                                else:
                                    if synthetic_override is not None:
                                        tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(
                                            job, db, file_paths_override=synthetic_override
                                        )
                                    else:
                                        tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(job, db)
                                    now_iso = datetime.datetime.utcnow().isoformat()
                                    new_attempts = attempts + 1
                                    if tracks_to_regen:
                                        overall_status = "running"
                                    disc_payload["previews"] = {
                                        "status": overall_status,
                                        "tracks": tracks_state,
                                        "updated_at": now_iso,
                                        "auto_recovery_attempts": new_attempts,
                                    }
                                    job.disc_payload = disc_payload
                                    db.commit()
                                    if tracks_to_regen:
                                        task_result = generate_previews.delay(str(job.id), tracks_to_regen)
                                        log.info(
                                            "Job %s: Re-enqueued generate_previews for %d track(s) (task_id=%s, auto_recovery_attempt=%s)",
                                            job.id,
                                            len(tracks_to_regen),
                                            task_result.id if task_result else "unknown",
                                            new_attempts,
                                        )
                                    else:
                                        log.info("Job %s: Preview recovery found nothing to regenerate", job.id)
                        else:
                            post_paths = getattr(job, "post_paths", None) or disc_payload.get("post_paths") or {}
                            ripped_files = getattr(job, "ripped_files", None) or disc_payload.get("ripped_files") or {}
                            file_paths = post_paths if post_paths else ripped_files
                            synthetic_override = None
                            if not file_paths:
                                existing_tracks = previews_block.get("tracks", {}) if isinstance(previews_block.get("tracks"), dict) else {}
                                if existing_tracks:
                                    synthetic_override = {str(k): None for k in existing_tracks.keys()}
                                    log.info(
                                        "Job %s: Using existing preview tracks for recovery (no file_paths on job)",
                                        job.id,
                                    )
                            if not file_paths and not synthetic_override:
                                log.warning(
                                    "Job %s: Cannot recover previews - no file paths or existing tracks",
                                    job.id,
                                )
                            else:
                                if synthetic_override is not None:
                                    tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(
                                        job, db, file_paths_override=synthetic_override
                                    )
                                else:
                                    tracks_state, tracks_to_regen, overall_status = build_preview_regeneration_state(job, db)
                                now_iso = datetime.datetime.utcnow().isoformat()
                                if tracks_to_regen:
                                    overall_status = "running"
                                disc_payload["previews"] = {
                                    "status": overall_status,
                                    "tracks": tracks_state,
                                    "updated_at": now_iso,
                                    "auto_recovery_attempts": 0,
                                }
                                disc_payload["previews"].pop("auto_recovery_last_error", None)
                                job.disc_payload = disc_payload
                                db.commit()
                                if tracks_to_regen:
                                    task_result = generate_previews.delay(str(job.id), tracks_to_regen)
                                    log.info(
                                        "Job %s: Re-enqueued generate_previews for %d track(s) (task_id=%s) after health check",
                                        job.id,
                                        len(tracks_to_regen),
                                        task_result.id if task_result else "unknown",
                                    )
                                else:
                                    log.info("Job %s: Preview recovery found nothing to regenerate", job.id)
                    except Exception as preview_exc:
                        log.error(
                            "Job %s: Failed to recover stuck preview generation: %s",
                            job.id,
                            preview_exc,
                            exc_info=True,
                        )
            # Handle orphaned jobs (Celery task not running)
            elif job.id in orphaned_job_map:
                # If this job is for the disc we're about to rip (start_rip called with disc context),
                # use superseded reason so job_state skips job_finished/context_changed and doesn't overwrite UI
                orphan_error_msg = error_msg
                if disc_hash or mount_point:
                    job_disc_hash = getattr(getattr(job, "disc", None), "content_hash", None)
                    job_mount = getattr(job, "mount_point", None)
                    if (disc_hash and job_disc_hash == disc_hash) or (mount_point and job_mount == mount_point):
                        orphan_error_msg = "Job superseded: previous attempt never started or did not run; starting new rip."
                        log.info(
                            "Job %s: Marking as superseded (same disc as start_rip) so new rip can proceed without emitting",
                            job.id,
                        )
                log.warning("Job %s: Marking as failed due to orphaned Celery task", job.id)
                try:
                    if getattr(job, "rip_state", None) not in ("completed", "skipped"):
                        StageState.rip_failed(
                            db, job,
                            error_reason=orphan_error_msg,
                            reason="orphaned Celery task (service restart)" if orphan_error_msg == error_msg else "superseded by new rip",
                        )
                    apply_job_state(
                        db, job,
                        updates={"workflow_step": _default_workflow_step(job)},
                        reason="workflow_step reset after rip failed",
                    )
                    # Emit disc context update after job status change
                    try:
                        import asyncio
                        try:
                            loop = asyncio.get_running_loop()
                            asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                        except RuntimeError:
                            from api.main import _app_instance
                            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                loop = _app_instance.state.event_loop
                                asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
                    except Exception as exc:
                        log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
                    failed_ids.append(str(job.id))
                    if JobPaths.for_id(str(job.id)).root.exists():
                        cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
                except Exception as fail_exc:
                    log.error("Job %s: Failed to mark orphaned job as failed: %s", job.id, fail_exc, exc_info=True)
            else:
                try:
                    if getattr(job, "rip_state", None) not in ("completed", "skipped"):
                        StageState.rip_failed(db, job, error_reason=error_msg, reason="stale job cleanup")
                    else:
                        apply_job_state(
                            db, job,
                            updates={"job_status": "failed", "error_reason": error_msg},
                            reason="stale job cleanup",
                        )
                    # No disc-level workflow_step reset; job.workflow_step is the source of truth
                    # Emit disc context update after job status change
                    try:
                        import asyncio
                        try:
                            loop = asyncio.get_running_loop()
                            asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                        except RuntimeError:
                            from api.main import _app_instance
                            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                loop = _app_instance.state.event_loop
                                asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
                    except Exception as exc:
                        log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
                    failed_ids.append(str(job.id))
                    if JobPaths.for_id(str(job.id)).root.exists():
                        cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
                except Exception as state_exc:
                    log.error("Job %s: Failed to apply state update: %s", job.id, state_exc, exc_info=True)
        except Exception as exc:
            # best-effort: don't block API calls due to cleanup failures, but also don't
            # silently apply unvalidated state transitions.
            log.warning("Failed to mark stale job %s failed: %s", getattr(job, "id", None), exc)
    return failed_ids


def _fail_jobs_for_disc(
    disc_hash: Optional[str] = None,
    db: Session = None,
    reason: str = "disc ejected",
    *,
    mount_point: Optional[str] = None,
) -> list[str]:
    """
    Mark all running/pending jobs for a specific disc as failed.
    Called when a disc is ejected to immediately fail any active jobs.

    Prefer disc_hash when available. If disc_hash is missing (e.g. cache was keyed by
    MakeMKV index and udev eject did not resolve hash), pass mount_point to fail jobs
    on that device only.
    """
    if not disc_hash and not mount_point:
        log.warning(
            "Cannot fail jobs for ejected disc: disc_hash and mount_point not available "
            "(disc_num alone is not unique)"
        )
        return []

    if disc_hash:
        running_jobs = (
            db.query(db_models.Job)  # type: ignore[attr-defined]
            .join(db_models.Disc)  # type: ignore[attr-defined]
            .filter(
                db_models.Disc.content_hash == disc_hash,  # type: ignore[attr-defined]
                db_models.Job.rip_state.in_(["pending", "running"]),  # type: ignore[attr-defined]
            )
            .all()
        )
    else:
        assert mount_point is not None
        running_jobs = (
            db.query(db_models.Job)  # type: ignore[attr-defined]
            .filter(
                db_models.Job.mount_point == mount_point,
                db_models.Job.rip_state.in_(["pending", "running"]),
            )
            .all()
        )

    failed_ids = []
    # Kill makemkvcon for this disc first (child process may outlive Celery worker on revoke)
    if running_jobs:
        first_job = running_jobs[0]
        disc_identifier = mount_point or getattr(first_job, "mount_point", None) or getattr(first_job, "disc_num", None)
        if disc_identifier:
            try:
                from core.utils import kill_makemkvcon_for_disc
                if kill_makemkvcon_for_disc(str(disc_identifier)):
                    log.info("Killed makemkvcon for disc %s (eject)", disc_identifier)
            except Exception as kill_exc:
                log.warning("Failed to kill makemkvcon for disc %s: %s", disc_identifier, kill_exc)
    for job in running_jobs:
        try:
            # Revoke Celery task so the worker stops ripping immediately
            celery_task_id = getattr(job, "celery_task_id", None)
            if celery_task_id:
                try:
                    from workers.tasks import celery_app
                    celery_app.control.revoke(celery_task_id, terminate=True)
                    log.info("Revoked Celery task %s for job %s (disc ejected)", celery_task_id, job.id)
                except Exception as revoke_exc:
                    log.warning("Failed to revoke Celery task %s for job %s: %s", celery_task_id, job.id, revoke_exc)
            error_msg = f"Job failed: {reason}"
            if getattr(job, "rip_state", None) not in ("completed", "skipped"):
                StageState.rip_failed(db, job, error_reason=error_msg, reason=reason)
            else:
                apply_job_state(
                    db, job,
                    updates={"job_status": "failed", "error_reason": error_msg},
                    reason=reason,
                )
            apply_job_state(
                db, job,
                updates={"workflow_step": _default_workflow_step(job)},
                reason="workflow_step reset after rip failed",
            )
            # Emit disc context update after job status change
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                except RuntimeError:
                    from api.main import _app_instance
                    if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                        loop = _app_instance.state.event_loop
                        asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
            except Exception as exc:
                log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
            failed_ids.append(str(job.id))
            log.info("Marked job %s as failed due to disc ejection (disc_hash=%s)", job.id, disc_hash)
        except Exception as exc:
            log.error("Failed to mark job %s as failed after disc ejection: %s", job.id, exc, exc_info=True)
    
    return failed_ids


def _collect_celery_tasks_on_workers() -> tuple[bool, set[str]]:
    """
    Task IDs Celery workers report as active, reserved, or scheduled.

    Returns:
        (inspect_replied, task_ids). If inspect_replied is False (e.g. inspect().active()
        is None because no worker responded), callers must not treat a missing task id as proof
        the task is dead — only skip orphan handling in that case.
    """
    from workers.tasks import celery_app

    ids: set[str] = set()
    try:
        insp = celery_app.control.inspect()
        if not insp:
            return False, ids
        active_tasks = insp.active()
        if active_tasks is None:
            return False, ids
        for tasks in active_tasks.values():
            for task in tasks:
                tid = task.get("id")
                if tid:
                    ids.add(tid)
        reserved_tasks = insp.reserved()
        if reserved_tasks:
            for tasks in reserved_tasks.values():
                for task in tasks:
                    tid = task.get("id")
                    if tid:
                        ids.add(tid)
        scheduled_tasks = insp.scheduled()
        if scheduled_tasks:
            for entries in scheduled_tasks.values():
                for entry in entries:
                    req = entry.get("request") or {}
                    tid = req.get("id")
                    if tid:
                        ids.add(tid)
        return True, ids
    except Exception as exc:
        log.warning("Could not collect Celery tasks from workers (inspect): %s", exc)
        return False, ids


def _rip_copy_finished_signals_in_db(job: Any) -> bool:
    """
    True when persisted job fields indicate MakeMKV copy finished, even if the worker
    never reached POST /rip-complete (e.g. service restart right after last title).
    """
    def _nonneg_int(val: Any) -> int | None:
        if val is None:
            return None
        try:
            i = int(val)
        except (TypeError, ValueError):
            return None
        if i < 0:
            return None
        return i

    rp = _nonneg_int(getattr(job, "rip_progress", None))
    if rp is not None and rp >= 100:
        return True
    tc = _nonneg_int(getattr(job, "titles_completed", None))
    tt = _nonneg_int(getattr(job, "total_titles", None))
    if tc is not None and tt is not None and tt > 0 and tc >= tt:
        return True
    ripped = getattr(job, "ripped_files", None)
    if not isinstance(ripped, dict):
        return False
    if tt is not None and tt > 0 and len(ripped) >= tt:
        return True
    return False


def _fail_orphaned_rip_jobs_on_startup(db: Session) -> list[str]:
    """
    On service startup, fail jobs that were actively ripping when the service stopped.

    If the Celery task and rip PID are gone but DB fields show copy already finished
    (progress/titles/ripped_files), reconcile by moving to rip_phase=verification and
    enqueueing rip_verification instead of failing and deleting MKVs.

    IMPORTANT: Only fails jobs where the Celery task is truly dead/orphaned.
    If the Celery worker is still running (common during backend-only restarts),
    the job is left alone to continue.

    This handles different restart scenarios:
    - Backend code reload (uvicorn auto-reload): Celery still running → jobs continue
    - Manual restart (manage.sh restart): Celery stopped → jobs marked as failed
      (or verification re-enqueued when copy-complete signals are present)
    - Production crash/restart: Depends on whether Celery is still running
    """
    # Find all jobs that were actively ripping
    orphaned_jobs = (
        db.query(db_models.Job)  # type: ignore[attr-defined]
        .join(db_models.Disc)  # type: ignore[attr-defined]
        .filter(
            db_models.Job.rip_state == "running"  # type: ignore[attr-defined]
        )
        .all()
    )
    
    if not orphaned_jobs:
        return []

    inspect_replied, worker_task_ids = _collect_celery_tasks_on_workers()
    if not inspect_replied:
        log.warning(
            "Startup recovery: Celery inspect did not return worker state - skipping orphaned rip failure "
            "(cannot tell dead tasks from unreachable workers)"
        )
        return []

    log.info(
        "Startup recovery: %d Celery task id(s) on workers (active/reserved/scheduled)",
        len(worker_task_ids),
    )

    failed_ids = []
    for job in orphaned_jobs:
        try:
            disc_hash = job.disc.content_hash if job.disc else None

            if _rip_operation_lock_held_for_job(job):
                log.info(
                    "Job %s: disc rip lock held — not marking as failed on startup (disc_hash=%s)",
                    job.id,
                    disc_hash,
                )
                continue

            celery_task_id = getattr(job, "celery_task_id", None)

            # If no task ID, assume orphaned (shouldn't happen for running jobs)
            if not celery_task_id:
                log.warning(
                    "Job %s: rip_state=running but no celery_task_id - assuming orphaned",
                    job.id,
                )
                # Mark as failed (code below)
            else:
                if celery_task_id in worker_task_ids:
                    log.info(
                        "Job %s: Celery task %s is on a worker (active/reserved/scheduled) - not marking as failed (disc_hash=%s)",
                        job.id,
                        celery_task_id,
                        disc_hash,
                    )
                    continue

                rip_pid = getattr(job, "rip_pid", None)
                if rip_pid is not None and is_pid_alive(rip_pid):
                    log.info(
                        "Job %s: rip_pid=%s is alive (makemkvcon) - not marking as failed (disc_hash=%s)",
                        job.id,
                        rip_pid,
                        disc_hash,
                    )
                    continue

                try:
                    from workers.tasks import celery_app
                    from celery.result import AsyncResult

                    task_state = AsyncResult(celery_task_id, app=celery_app).state
                    log.info(
                        "Job %s: Celery task %s not on workers; result backend state=%s; marking orphaned (disc_hash=%s)",
                        job.id,
                        celery_task_id,
                        task_state,
                        disc_hash,
                    )
                except Exception as exc:
                    log.warning(
                        "Job %s: Could not read Celery result for %s (%s) - marking orphaned",
                        job.id,
                        celery_task_id,
                        exc,
                    )

            # Task is truly dead or missing — recover if copy already finished in DB
            if _rip_copy_finished_signals_in_db(job):
                phase = getattr(job, "rip_phase", None)
                if phase in (None, "copy", "verification"):
                    try:
                        if phase in (None, "copy"):
                            StageState.rip_copy_complete(
                                db,
                                job,
                                reason="startup orphan: copy complete in DB, re-enqueue verification",
                            )
                        enqueue_rip_verification_for_job(
                            str(job.id),
                            reason="startup orphan heal: rip task lost after copy",
                        )
                        log.info(
                            "Startup recovery: job %s reconciled (copy complete in DB, task/PID gone); "
                            "enqueued rip_verification (disc_hash=%s)",
                            job.id,
                            disc_hash,
                        )
                        try:
                            import asyncio

                            try:
                                loop = asyncio.get_running_loop()
                                asyncio.create_task(
                                    _emit_disc_context_when_job_updates(str(job.id), db)
                                )
                            except RuntimeError:
                                from api.main import _app_instance

                                if (
                                    _app_instance
                                    and hasattr(_app_instance, "state")
                                    and hasattr(_app_instance.state, "event_loop")
                                ):
                                    loop = _app_instance.state.event_loop
                                    asyncio.run_coroutine_threadsafe(
                                        _emit_disc_context_when_job_updates(str(job.id), db),
                                        loop,
                                    )
                        except Exception as exc:
                            log.warning(
                                "Failed to emit disc context update for healed job %s: %s",
                                job.id,
                                exc,
                            )
                        continue
                    except Exception as exc:
                        log.warning(
                            "Startup recovery: could not heal job %s via rip_verification (%s) — failing job",
                            job.id,
                            exc,
                        )

            # Mark job as failed (incomplete rip or heal enqueue failed)
            error_msg = "Job failed: service restart detected (rip process no longer running)"
            StageState.rip_failed(db, job, error_reason=error_msg, reason="startup: service restart detected")
            apply_job_state(
                db, job,
                updates={"workflow_step": _default_workflow_step(job)},
                reason="workflow_step reset after rip failed",
            )

            # Emit disc context update after job status change
            try:
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
                except RuntimeError:
                    from api.main import _app_instance
                    if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                        loop = _app_instance.state.event_loop
                        asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
            except Exception as exc:
                log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")

            failed_ids.append(str(job.id))
            if JobPaths.for_id(str(job.id)).root.exists():
                cleanup_job_mkv.delay(str(job.id), "stale_cleanup")
            log.info("Marked orphaned job %s as failed on startup (disc_hash=%s)", job.id, disc_hash)
        except Exception as exc:
            log.error("Failed to mark orphaned job %s as failed on startup: %s", job.id, exc, exc_info=True)
    
    if failed_ids:
        log.info("Startup recovery: Marked %d orphaned job(s) as failed: %s", len(failed_ids), failed_ids)
    else:
        log.info("Startup recovery: No orphaned jobs found (all active jobs have running Celery tasks)")
    
    return failed_ids


def _handle_orphaned_task(task_id: str, db: Session) -> None:
    """
    Handle an orphaned Celery task (task exists but no job found in Postgres).
    Revokes the orphaned task to clean it up.
    """
    try:
        from workers.tasks import celery_app
        
        log.warning("Found orphaned task %s with no associated job - revoking", task_id)
        celery_app.control.revoke(task_id, terminate=True)
        log.info("Successfully revoked orphaned task %s", task_id)
    except Exception as exc:
        log.error("Failed to revoke orphaned task %s: %s", task_id, exc)


@router.post("/rip", response_model=JobStatus)
def start_rip(req: JobCreate, db: Session = Depends(get_db)):
    """
    Start a rip operation. Frontend provides mount_point and optional disc_id/disc_hash.
    Backend creates job and emits job_id through workflow context.
    """
    if not req.mount_point:
        raise HTTPException(status_code=400, detail="mount_point is required")
    
    rip_request_id = uuid.uuid4().hex
    
    # Validate MakeMKV installation before attempting to rip
    from core.makemkv_updater import validate_makemkv_installation
    makemkv_validation = validate_makemkv_installation()
    if not makemkv_validation["can_rip"]:
        log.error(
            "Cannot start rip: MakeMKV is not properly installed. rid=%s validation=%s",
            rip_request_id, makemkv_validation
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": "MakeMKV is not properly installed",
                "missing_components": makemkv_validation["missing_components"],
                "error_message": makemkv_validation["error_message"],
                "action_required": "reinstall_makemkv",
            }
        )
    
    # #724: drive-health gate. When the target drive was recorded as not
    # responding (mount timed out — see core.drive_health), refuse here with
    # the actionable message instead of letting the rip start against a drive
    # whose disc identity we could not read. Runs before every other gate so
    # the user gets "power cycle the drive", not a misleading
    # "disc_scan_in_progress" from the empty-cache precondition below.
    from core.drive_health import get_drive_health

    drive_health = get_drive_health(req.mount_point)
    if drive_health is not None:
        log.warning(
            "POST /jobs/rip rid=%s blocked: drive %s is unhealthy (%s)",
            rip_request_id, req.mount_point, drive_health.code,
        )
        try:
            from core.drive_health import (
                FAULT_NOTIFICATION_LEVEL,
                fault_notification_id_key,
            )
            from core.notifications import emit_notification_sync

            emit_notification_sync(
                message=(
                    f"Copy not started — {req.mount_point}: {drive_health.message}"
                ),
                kind="error",
                level=FAULT_NOTIFICATION_LEVEL,
                title="Drive is not responding",
                # Repeat clicks on a drive that is still down collapse into one
                # bell/Discord alert; the 409 below is what gives the user
                # immediate per-click feedback in the UI.
                id_key=fault_notification_id_key(
                    drive_health.code, req.mount_point, scope="rip_blocked"
                ),
            )
        except Exception as notif_exc:
            log.warning(
                "POST /jobs/rip rid=%s failed to emit drive-health notification: %s",
                rip_request_id, notif_exc,
            )
        raise HTTPException(
            status_code=409,
            detail={
                "error": drive_health.message,
                "code": drive_health.code,
                "mount_point": req.mount_point,
            },
        )

    # Resolve disc_hash and disc_num before cleanup so we can mark same-disc orphaned jobs as "superseded"
    # (avoids emitting job_finished/context_changed for the old job and overwriting the new job's context)
    disc_hash = None
    disc_num: str | None = req.disc_num
    disc_record = None
    if req.disc_id and not req.disc_id.startswith("pending-"):
        disc_record = db.query(db_models.Disc).filter(db_models.Disc.id == req.disc_id).first()
        if disc_record:
            disc_hash = disc_hash or getattr(disc_record, "content_hash", None)
            if not disc_num:
                disc_num = getattr(disc_record, 'disc_num', None)
    if not disc_hash:
        try:
            from core.disc_manager import get_cached_discs
            cached_discs = get_cached_discs()
            disc_info = next((d for d in cached_discs if d.get("mount_point") == req.mount_point), None)
            if disc_info:
                disc_hash = disc_info.get("disc_hash") or disc_info.get("content_hash")
                if not disc_num:
                    disc_num = disc_info.get("disc_num")
        except Exception as exc:
            log.warning("Could not get disc info from disc manager cache: %s", exc)
    if not disc_num:
        try:
            from core.disc_manager import list_drives
            drives = list_drives()
            for d in drives:
                drive_mount_point = d.get("mount_point") if isinstance(d, dict) else None
                drive_disc_num = d.get("disc_num") if isinstance(d, dict) else None
                if drive_mount_point == req.mount_point:
                    disc_num = str(drive_disc_num)
                    log.info("Found disc_num %s from drive list for mount_point %s", disc_num, req.mount_point)
                    break
        except Exception as exc:
            log.warning("Could not get disc_num from drive list: %s", exc)

    # NOTE (#560): Do NOT call _cleanup_stale_jobs on the rip-start request
    # path. The periodic background task at
    # ``Backend/api/main.py:_periodic_stale_job_cleanup`` runs the same logic
    # every N seconds. Calling it inline here races with concurrent rip
    # starts: when the user clicks Rip on drive B while drive A's task is
    # still in prep (no ``rip_pid`` yet, lock not yet acquired,
    # ``celery.inspect`` may miss the recently-received task), the cleanup
    # marks drive A's task as a "service restart" orphan and fails it.
    log.info("POST /jobs/rip rid=%s payload=%s", rip_request_id, req.dict())

    # #576: re-resolve mount_point from disc_hash + the live disc_cache before
    # consulting any gatekeeper. The frontend's card carries the mount_point
    # that was current when the card was first rendered — but USB drives
    # routinely renumber across hot-plug, so by the time the user clicks Rip
    # the disc may now live at a different ``/dev/srN``. The disc_cache always
    # carries the freshest mount_point because it's repopulated on every
    # successful insert scan via ``handle_disc_insert``.
    requested_mount_point = req.mount_point
    if disc_hash:
        from core.disc_cache import get as _cache_get
        cached_by_hash = _cache_get(disc_hash)
        cached_mp = (cached_by_hash or {}).get("mount_point")
        if cached_mp and cached_mp != requested_mount_point:
            log.info(
                "POST /jobs/rip rid=%s detected drive renumbering: requested=%s resolved=%s (via disc_hash=%s)",
                rip_request_id, requested_mount_point, cached_mp, disc_hash,
            )
            req.mount_point = cached_mp

    # #576: a Disc row with scan_state='failed' from a prior session blocks the
    # gatekeeper even when the current disc_cache has a fresh payload — the
    # failure flag is stale. Clear it before the gate runs. The next failed
    # scan will repopulate the column truthfully; we only clear when the
    # cache + DB agree on the content_hash, so we won't silently mask a real
    # current-session failure.
    if disc_record is not None and disc_hash:
        from core.disc_cache import get as _cache_get
        cached_for_clear = _cache_get(req.mount_point) or _cache_get(disc_hash)
        cache_has_fresh_hash = (
            cached_for_clear is not None
            and cached_for_clear.get("disc_hash") == disc_hash
        )
        if cache_has_fresh_hash and getattr(disc_record, "scan_state", None) == "failed":
            log.info(
                "POST /jobs/rip rid=%s clearing stale Disc.scan_state='failed' for disc_id=%s; live cache has matching disc_hash=%s",
                rip_request_id, disc_record.id, disc_hash,
            )
            disc_record.scan_state = None
            disc_record.last_scan_error = None
            db.commit()

    if not disc_num:
        raise HTTPException(status_code=400, detail="disc_num is required or could not be determined")

    # #562 PR 5: cache-precondition gate. If the disc-info cache has no
    # entry for this drive AND the request didn't bring a payload with a
    # ``disc_hash``, the rip task would otherwise open the disc inline to
    # populate the cache — which races a sibling drive's ``mkv dev:`` and
    # emits MSG:5010. Defer: enqueue ``discinfo_scan`` and return 409 with
    # ``{code, mount_point}`` so the UI can retry once the scan completes.
    from core.disc_scan_dispatch import (
        disc_info_cache_satisfies,
        enqueue_discinfo_scan,
    )

    # JobCreate has no ``disc_payload`` field (only JobStatus does); the gate's
    # third arg is reserved for callers that already have a hydrated payload
    # — pass None here so the gate falls through to the disc_cache lookup.
    if not disc_info_cache_satisfies(req.mount_point, disc_num, None):
        task_id = enqueue_discinfo_scan(disc_num, req.mount_point)
        log.info(
            "POST /jobs/rip rid=%s deferring: disc_scan_in_progress mount_point=%s task_id=%s",
            rip_request_id, req.mount_point, task_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "disc_scan_in_progress",
                "mount_point": req.mount_point,
                "discinfo_scan_task_id": task_id,
            },
        )

    # Path A trigger gate: on Midway-class obfuscated discs (duplicate
    # sorted-segment-map groups present AND projected rip > threshold), defer
    # to the frontend modal instead of starting the rip. Skipped when the
    # request explicitly opts out via force_full_rip (user already saw the
    # modal and picked "Rip whole disc anyway").
    force_full_rip = bool(getattr(req, "force_full_rip", False))
    if not force_full_rip:
        try:
            from core.disc_manager import get_cached_discs as _get_cached_discs
            from core.path_a_trigger import evaluate_path_a_trigger

            cached = _get_cached_discs() or []
            disc_info = next(
                (d for d in cached if d.get("mount_point") == req.mount_point),
                None,
            )
            titles_for_gate = (disc_info or {}).get("titles") or {}
            disc_size_bytes = (disc_info or {}).get("disc_size_bytes")
            decision = evaluate_path_a_trigger(titles_for_gate, disc_size_bytes)
            if decision.needs_user_choice:
                log.info(
                    "POST /jobs/rip rid=%s deferring to Path A modal: %s",
                    rip_request_id, decision.reason,
                )
                # Probe free space on the rip output volume so the modal can
                # disable "Rip whole disc anyway" when it won't fit. Best-effort —
                # if the probe fails we fall through with None (modal stays
                # permissive and the backend's pre-flight catches it later).
                available_disk_bytes: int | None = None
                try:
                    import shutil
                    from core.utils import get_mkvauto_data
                    available_disk_bytes = shutil.disk_usage(str(get_mkvauto_data())).free
                except Exception as exc:
                    log.warning(
                        "POST /jobs/rip rid=%s could not probe free disk for 409: %s",
                        rip_request_id, exc,
                    )
                payload = decision.to_409_payload(available_disk_bytes=available_disk_bytes)
                raise HTTPException(status_code=409, detail=payload)
        except HTTPException:
            raise
        except Exception as exc:
            # Best-effort gate; fail-open to preserve today's behavior on errors.
            log.warning(
                "POST /jobs/rip rid=%s Path A gate evaluation failed (fail-open): %s",
                rip_request_id, exc,
            )
    
    # Multi-drive policy gate (#540): refuse rips on drives whose identity
    # falls back below /dev/disk/by-id/, when other drives are also attached.
    # Stable Decision codes let the frontend render a contextual banner.
    try:
        from core.drive_identity import build_identity_map
        from core.drive_policy import evaluate_drive_for_rip

        identity_map = build_identity_map()
        target_identity = identity_map.get(req.mount_point)
        if target_identity is not None:
            decision = evaluate_drive_for_rip(
                target_identity,
                all_drives=list(identity_map.values()),
            )
            if not decision.allowed:
                log.warning(
                    "POST /jobs/rip rid=%s blocked by multi-drive policy: code=%s mount_point=%s",
                    rip_request_id, decision.code, req.mount_point,
                )
                # Emit a unified notification so both Discord and the WebUI
                # toast surface the policy rejection through the same channel
                # as every other backend-originated alert. The frontend's
                # startRip subscription still observes the 409 and can clear
                # its loading state, but no longer needs a local toast call.
                try:
                    from core.notifications import emit_notification_sync
                    emit_notification_sync(
                        message=decision.message or "Multi-drive policy blocked the rip.",
                        kind="warning",
                        level="action_required",
                        title="Drive not supported for concurrent rips",
                        id_key=f"drive_policy:{decision.code}:{req.mount_point}",
                    )
                except Exception as notif_exc:
                    log.warning(
                        "POST /jobs/rip rid=%s failed to emit policy notification: %s",
                        rip_request_id, notif_exc,
                    )
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": decision.message,
                        "code": decision.code,
                        "mount_point": req.mount_point,
                    },
                )
    except HTTPException:
        raise
    except Exception as policy_exc:
        # Fail-open if the identity layer itself errors — preserves today's
        # behaviour on systems where /dev/disk/by-id is unavailable.
        log.warning(
            "POST /jobs/rip rid=%s multi-drive policy evaluation failed (fail-open): %s",
            rip_request_id, policy_exc,
        )

    # #578: USB-bus-saturation policy gate. When the target drive is on a
    # sub-SuperSpeed USB bus that already hosts an active rip, refuse with
    # a structured 409 so the frontend can show a confirmation modal that
    # lets the user override via ``force_concurrent_on_saturated_bus``.
    # This is the defense-in-depth complement to the startup notification
    # surfaced in Settings (#582) — users see the contention warning at
    # boot, but if they click Rip anyway the backend catches it.
    try:
        from core.usb_bus_saturation_policy import evaluate_bus_saturation

        sat_decision = evaluate_bus_saturation(
            req.mount_point,
            db,
            force_override=bool(getattr(req, "force_concurrent_on_saturated_bus", False)),
        )
        if not sat_decision.allowed:
            log.warning(
                "POST /jobs/rip rid=%s blocked by USB bus saturation: bus=%s "
                "speed=%sMbps competing=%s",
                rip_request_id, sat_decision.bus, sat_decision.speed_mbps,
                list(sat_decision.competing_mount_points),
            )
            raise HTTPException(
                status_code=409,
                detail=sat_decision.to_409_payload(),
            )
    except HTTPException:
        raise
    except Exception as sat_exc:
        log.warning(
            "POST /jobs/rip rid=%s USB saturation gate failed (fail-open): %s",
            rip_request_id, sat_exc,
        )

    # If disc_hash is still not available, we'll compute it during the rip
    # The gatekeeper will handle fetching disc info and computing hash if needed
    if not disc_hash:
        log.info("No disc_hash provided, will be computed during rip for disc %s at %s", disc_num, req.mount_point)

    # Use DriveGatekeeper to create job (it will fetch disc info and compute hash if needed)
    # can_start_rip will check by mount_point if disc_hash is empty
    log.info(
        "POST /jobs/rip rid=%s calling can_start_rip disc_hash=%s disc_num=%s mount_point=%s",
        rip_request_id, disc_hash or "(empty)", disc_num, req.mount_point
    )
    gatekeeper = DriveGatekeeper(db)
    can_start, existing_job = gatekeeper.can_start_rip(disc_hash or "", disc_num, req.mount_point, rip_request_id=rip_request_id)
    log.info(
        "POST /jobs/rip rid=%s can_start_rip returned can_start=%s existing_job=%s (id=%s status=%s rip_state=%s)",
        rip_request_id, can_start, existing_job is not None,
        existing_job.id if existing_job else None,
        existing_job.job_status if existing_job else None,
        getattr(existing_job, "rip_state", None) if existing_job else None
    )
    
    if not can_start:
        if existing_job:
            log.warning(
                "POST /jobs/rip rid=%s blocked: existing_job=%s disc_num=%s mount_point=%s disc_hash=%s "
                "job_status=%s rip_state=%s celery_task_id=%s rip_pid=%s",
                rip_request_id, existing_job.id, disc_num, req.mount_point, disc_hash,
                existing_job.job_status, getattr(existing_job, "rip_state", None),
                getattr(existing_job, "celery_task_id", None), getattr(existing_job, "rip_pid", None)
            )
            job_id = str(existing_job.id)
            # If existing job is pending and its rip task is not really running, dispatch so makemkvcon can run.
            # Use is_rip_task_really_running (PENDING = inactive) so we re-dispatch after backend/worker restart
            # when the old task is stuck as PENDING in the result backend.
            from core.drive_gatekeeper import is_rip_task_really_running
            job_status = getattr(existing_job, "job_status", None)
            if job_status == "pending" and not is_rip_task_really_running(existing_job):
                try:
                    out_dir = getattr(existing_job, "output_dir", None) or req.output_dir
                    task_result = rip_disc.apply_async(
                        args=(job_id, disc_num, req.mount_point, req.mode or "copy", out_dir),
                        kwargs={"rip_request_id": rip_request_id},
                        task_id=f"rip_disc:{job_id}",
                    )
                    existing_job.celery_task_id = task_result.id
                    db.commit()
                    # Optimistic state update so UI shows "Copy in progress" immediately (task may sit in queue)
                    try:
                        StageState.rip_started(db, existing_job, reason="rip_redispatch")
                    except Exception as state_exc:
                        log.warning("POST /jobs/rip rid=%s optimistic state update failed: %s", rip_request_id, state_exc)
                    log.info(
                        "POST /jobs/rip rid=%s re-dispatched rip_disc for pending job=%s task_id=%s",
                        rip_request_id, job_id, task_result.id,
                    )
                except Exception as dispatch_exc:
                    log.warning("POST /jobs/rip rid=%s failed to re-dispatch for pending job=%s: %s", rip_request_id, job_id, dispatch_exc)
            # Emit workflow context update
            disc_id_for_context = req.disc_id if req.disc_id and not req.disc_id.startswith("pending-") else None
            _emit_workflow_context_after_job_creation(job_id, disc_id_for_context, db)
            # Persist workflow_step on job and return JobStatus (POST-driven step; frontend applies response)
            job = crud.get_job(db, job_id)
            if not job:
                raise HTTPException(404, detail="Job not found")
            profile = _workflow_profile_for_steps(job)
            if profile == "hit":
                job.workflow_step = "summary"
            else:
                job.workflow_step = "boxset"
            db.commit()
            db.refresh(job)
            status = _build_job_status(job, job_created=False)
            return status
        else:
            log.error(
                "POST /jobs/rip rid=%s 409 CONFLICT: duplicate check failed without existing_job. "
                "can_start=False but existing_job=None. This should not happen. "
                "disc_num=%s mount_point=%s disc_hash=%s",
                rip_request_id, disc_num, req.mount_point, disc_hash
            )
            # Log additional diagnostic info
            try:
                from core.drive_gatekeeper import is_rip_running_for_disc
                is_running_diag, active_job_diag = is_rip_running_for_disc(db, disc_hash, disc_num, req.mount_point)
                log.error(
                    "POST /jobs/rip rid=%s diagnostic: is_rip_running_for_disc returned is_running=%s active_job=%s",
                    rip_request_id, is_running_diag, active_job_diag.id if active_job_diag else None
                )
            except Exception as diag_exc:
                log.error("POST /jobs/rip rid=%s diagnostic check failed: %s", rip_request_id, diag_exc, exc_info=True)
            raise HTTPException(status_code=409, detail="Cannot start rip: duplicate check failed")
    
    # Pre-flight disk space validation
    try:
        # Get disc info for size calculation (similar to what gatekeeper does)
        disc_info = None
        try:
            from core.disc_manager import get_cached_discs
            cached_discs = get_cached_discs()
            for disc in cached_discs:
                if disc.get("disc_num") == str(disc_num) and disc.get("mount_point") == req.mount_point:
                    disc_info = disc
                    break
            
            if not disc_info and disc_hash:
                # Try getting from gatekeeper's get_disc_info
                try:
                    disc_info = gatekeeper.get_disc_info(disc_hash, disc_num, req.mount_point, refresh=False)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Could not get disc info for space check: %s", exc)
        
        titles = disc_info.get("titles") if disc_info else None
        disc_size_bytes = None
        if disc_info and disc_info.get("disc_size_bytes"):
            disc_size_bytes = disc_info.get("disc_size_bytes")
        elif disc_record and disc_record.disc_size_bytes:
            disc_size_bytes = disc_record.disc_size_bytes

        required_bytes = calculate_required_rip_space_bytes(titles, disc_size_bytes, buffer_multiplier=1.3)
        if required_bytes:
            # Resolve output directory
            output_dir = req.output_dir
            if output_dir:
                check_dir = str(resolve_jobs_root(output_dir))
            else:
                check_dir = str(resolve_jobs_root(None))

            # Check available space
            is_sufficient, error_msg = check_disk_space_for_rip(check_dir, required_bytes)

            if not is_sufficient:
                log.error("Pre-flight disk space check failed: %s", error_msg)
                try:
                    from core.notifications import emit_notification_sync
                    emit_notification_sync(error_msg, "error", "error_disk_space")
                except Exception as notify_exc:
                    log.warning("Failed to emit disk space notification: %s", notify_exc)

                raise HTTPException(status_code=400, detail=error_msg)
        else:
            log.info("Disc size could not be calculated from titles or disc size fallback, skipping pre-flight space check")
    except HTTPException:
        # Re-raise HTTP exceptions (our validation errors)
        raise
    except Exception as exc:
        # Log but don't fail - let the worker's MIN_OUTPUT_FREE_BYTES check handle it
        log.warning("Pre-flight disk space check failed with exception (continuing anyway): %s", exc)
    
    # #638: defence-in-depth. Reject rip dispatch when the target disc has no
    # enumerated titles — the info scan raced with cleanup and produced an empty
    # tracklist. Without this gate the rip runs for 20+ min then fails at
    # rip_verification with "no MKV outputs found under raw/".
    disc_for_titles_check = disc_record
    if disc_for_titles_check is None and disc_hash:
        disc_for_titles_check = (
            db.query(db_models.Disc)
            .filter(db_models.Disc.content_hash == disc_hash)
            .first()
        )
    if disc_for_titles_check is not None:
        title_count = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.disc_id == disc_for_titles_check.id)
            .count()
        )
        if title_count == 0:
            log.warning(
                "POST /jobs/rip rid=%s blocked: disc=%s has 0 disc_titles rows "
                "(scan_state=%s, format=%s). Refusing dispatch — user must eject+reinsert.",
                rip_request_id, disc_for_titles_check.id,
                getattr(disc_for_titles_check, "scan_state", None),
                getattr(disc_for_titles_check, "format", None),
            )
            # #720: report WHY the scan produced no titles. The old blanket
            # "eject and reinsert" is wrong (and infuriating) when the scan
            # actually ran and failed — e.g. makemkv couldn't decrypt the disc
            # (copy-protection/key-exchange failure) or the disc is unreadable.
            # Reseating can't fix those, so say what really happened.
            scan_err = (getattr(disc_for_titles_check, "last_scan_error", None) or "").strip()
            never_scanned = (
                getattr(disc_for_titles_check, "disc_info", None) is None
                and not (getattr(disc_for_titles_check, "scan_attempts", 0) or 0)
            )
            if scan_err:
                low = scan_err.lower()
                if "key" in low or "copy protection" in low or "css" in low or "aacs" in low:
                    msg = (
                        "The disc scan failed: MakeMKV could not decrypt this disc "
                        f"({scan_err}). This is usually a dirty/scratched disc, or a disc "
                        "this drive can't decrypt — reinserting won't help. Try cleaning the "
                        "disc, or another drive."
                    )
                else:
                    msg = f"The disc scan failed: {scan_err}"
            elif never_scanned:
                msg = (
                    "This disc has not been scanned yet, so no titles are known. "
                    "Rescan the disc (eject and reinsert, or trigger a rescan); if that "
                    "doesn't help, check the MakeMKV log for read errors."
                )
            else:
                msg = (
                    "Disc scan completed but enumerated no titles. The disc may be "
                    "unreadable, empty, or an unsupported format — check the MakeMKV log."
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "disc_scan_incomplete",
                    "error": msg,
                    "disc_id": str(disc_for_titles_check.id),
                    "scan_error": scan_err or None,
                    "never_scanned": never_scanned,
                },
            )

    try:
        # Log API endpoint entry point with call context
        import traceback
        call_stack = ''.join(traceback.format_stack()[-4:-1])  # Last 3 frames (excluding this one)
        log.info(
            "API /jobs/rip endpoint calling gatekeeper.start_rip rid=%s disc_num=%s mount_point=%s disc_hash=%s mode=%s call_stack=%s",
            rip_request_id, disc_num, req.mount_point, disc_hash or "empty", req.mode,
            call_stack.replace('\n', ' | ')
        )
        # Pass disc_hash if available, otherwise pass empty string and let gatekeeper compute it
        job = gatekeeper.start_rip(
            disc_hash=disc_hash or "",  # Pass empty string if not available, gatekeeper will compute
            disc_num=disc_num,
            mount_point=req.mount_point,
            mode=req.mode,
            output_dir=req.output_dir,
            payload=None,  # Gatekeeper will fetch disc info
            rip_request_id=rip_request_id
        )
        job_id = str(job.id)
        log.info(
            "Rip started successfully: rid=%s job=%s disc_num=%s hash=%s mount_point=%s",
            rip_request_id, job_id, disc_num, disc_hash or "computed", req.mount_point
        )
        
        # Refresh so we see the gatekeeper's optimistic update (job_status/rip_state running)
        db.refresh(job)
        
        # Emit workflow context update with job_id
        # Workflow context (labelForm) is already saved to disc via PATCH /discs/{disc_id}/workflow-context
        # before job creation, so we don't need to apply it here
        disc_id_for_context = req.disc_id if req.disc_id and not req.disc_id.startswith("pending-") else (job.disc_id if job.disc_id else None)
        _emit_workflow_context_after_job_creation(job_id, disc_id_for_context, db)

        # Persist workflow_step on job and return JobStatus (POST-driven step; frontend applies response)
        profile = _workflow_profile_for_steps(job)
        if profile == "hit":
            job.workflow_step = "summary"
        else:
            job.workflow_step = "boxset"
        db.commit()
        db.refresh(job)
        status = _build_job_status(job, job_created=True)
        # Ensure frontend shows "Copy in progress" immediately: if we just dispatched and status is still pending, override
        if getattr(job, "celery_task_id", None) and (status.job_status == "pending" or getattr(status, "rip_state", None) == "pending"):
            status = status.model_copy(update={"job_status": "running", "rip_state": "running"})
        return status
    except ValueError as exc:
        log.warning("Gatekeeper validation error: %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        log.error("Failed to start rip via gatekeeper: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Failed to start rip: {exc}") from exc


async def _emit_disc_context_when_job_updates(job_id: str, db: Session) -> None:
    """
    Helper to emit disc context change notification when job data changes.
    Called after job status/progress updates.
    """
    try:
        job = crud.get_job(db, job_id)
        if job and job.disc_id:
            from api.routers.websockets import _emit_to_disc_workflow
            await _emit_to_disc_workflow(str(job.disc_id), changed_fields=['jobStatus'])
    except Exception as exc:
        log.warning(f"Failed to emit disc context change notification for job {job_id}: {exc}")


def _emit_workflow_context_after_job_creation(job_id: str, disc_id: str | None, db: Session) -> None:
    """Emit workflow context change notification via WebSocket after job creation."""
    try:
        import asyncio
        from api.routers.websockets import _emit_to_job_workflow, _emit_to_disc_workflow
        
        # Emit to job workflow context (always)
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_job_workflow(job_id, changed_fields=['jobStatus', 'labelForm']))
        except RuntimeError:
            # No running loop - try to get app reference
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(_emit_to_job_workflow(job_id, changed_fields=['jobStatus', 'labelForm']), loop)
                else:
                    log.warning("No event loop available for workflow context emission")
            except Exception as exc:
                log.warning(f"Failed to schedule websocket emission for job {job_id}: {exc}")
        
        # If disc_id available, also emit to disc workflow context and coordinator
        if disc_id:
            try:
                from api.routers.websockets import _emit_disc_updated_with_job
                
                # Properly handle async call from sync context
                async def _emit_disc_notifications():
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.create_task(_emit_to_disc_workflow(disc_id, changed_fields=['jobStatus']))
                        # Also emit coordinator update with job_id
                        asyncio.create_task(_emit_disc_updated_with_job(disc_id, job_id))
                    except RuntimeError:
                        try:
                            from api.main import _app_instance
                            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                loop = _app_instance.state.event_loop
                                asyncio.run_coroutine_threadsafe(_emit_to_disc_workflow(disc_id, changed_fields=['jobStatus']), loop)
                                asyncio.run_coroutine_threadsafe(_emit_disc_updated_with_job(disc_id, job_id), loop)
                            else:
                                log.warning("No event loop available for disc workflow context emission")
                        except Exception as exc:
                            log.warning(f"Failed to schedule websocket emission for disc {disc_id}: {exc}")
                
                # Try to run in existing event loop, or create new one
                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(_emit_disc_notifications())
                except RuntimeError:
                    # No running loop - try to get app reference
                    try:
                        from api.main import _app_instance
                        if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                            loop = _app_instance.state.event_loop
                            asyncio.run_coroutine_threadsafe(_emit_disc_notifications(), loop)
                        else:
                            # Fallback: create new event loop (last resort)
                            asyncio.run(_emit_disc_notifications())
                    except Exception as exc:
                        log.warning(f"Failed to get disc workflow context for disc {disc_id}: {exc}")
            except Exception as exc:
                log.warning(f"Failed to emit disc workflow context for disc {disc_id}: {exc}")
    except Exception as exc:
        log.warning("Failed to emit workflow context update after job creation: %s", exc)
        # Don't fail job creation if context emission fails


@router.get("/unified", response_model=JobStatus)
async def get_unified_job_status(
    job_id: str | None = Query(None, description="Job ID (UUID)"),
    disc_id: str | None = Query(None, description="Disc ID to find job for"),
    disc_hash: str | None = Query(None, description="Disc hash to find job for"),
    include: str | None = Query(None, description="Comma-separated list: workflow,artifacts,previews,logs"),
    db: Session = Depends(get_db),
):
    """
    Unified endpoint for job status.
    Supports querying by job_id, disc_id, or disc_hash.
    Use 'include' parameter to control data depth:
    - workflow: Include workflow context (labelForm, etc.)
    - artifacts: Include full artifacts
    - previews: Include preview status
    - logs: Include full logs (default: last 100 lines)
    
    Example: /jobs/unified?job_id=xxx&include=workflow,artifacts
    """
    # _cleanup_stale_jobs removed from request path — runs as periodic background task
    include_set = set((include or "").split(",")) if include else set()
    
    job = None
    
    # Find job by various methods
    if job_id:
        job = crud.get_job(db, job_id)
    elif disc_id:
        disc = (
            db.query(db_models.Disc)
            .options(
                joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
                joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
                # selectinload (separate IN-query) avoids the Cartesian
                # explosion that joinedload causes when combined with the
                # release+movie+boxset joins on a 222-title disc.
                selectinload(db_models.Disc.titles),
            )
            .filter(db_models.Disc.id == disc_id)
            .first()
        )
        if disc:
            job = crud.get_active_job_for_disc(db, None, disc.content_hash)
            if not job:
                # Get most recent job for this disc
                job = (
                    db.query(crud.models.Job)  # type: ignore[attr-defined]
                    .options(joinedload(crud.models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.movie))
                    .filter(crud.models.Job.disc_id == disc_id)
                    .order_by(crud.models.Job.created_at.desc())
                    .first()
                )
    elif disc_hash:
        disc = (
            db.query(db_models.Disc)
            .options(
                joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
                joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
                # selectinload (separate IN-query) avoids the Cartesian
                # explosion that joinedload causes when combined with the
                # release+movie+boxset joins on a 222-title disc.
                selectinload(db_models.Disc.titles),
            )
            .filter(db_models.Disc.content_hash == disc_hash)
            .first()
        )
        if disc:
            job = crud.get_active_job_for_disc(db, None, disc_hash)
            if not job:
                # Get most recent job for this disc
                job = (
                    db.query(crud.models.Job)  # type: ignore[attr-defined]
                    .options(joinedload(crud.models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.movie))
                    .filter(crud.models.Job.disc_id == disc.id)
                    .order_by(crud.models.Job.created_at.desc())
                    .first()
                )
    
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    
    return _build_job_status(job)


@router.get("/{job_id}/status", response_model=JobStatus)
def get_status(job_id: str, db: Session = Depends(get_db)):
    """
    Get the current status of a job by ID.
    DEPRECATED: Use /jobs/unified?job_id=xxx instead.
    """
    # _cleanup_stale_jobs removed from request path — runs as periodic background task
    log.info("GET /jobs/%s/status", job_id)
    job = crud.get_job(db, job_id)
    if not job:
        log.warning("Job %s not found", job_id)
        raise HTTPException(404, detail="Job not found")
    return _build_job_status(job)


# REMOVED: /jobs/current endpoint - replaced by Workflow Coordinator's unfinished_jobs list


@router.get("/{job_id}/artifacts", response_model=JobArtifacts)
def get_artifacts(job_id: str, db: Session = Depends(get_db)):
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    return JobArtifacts(
        jobId=str(job.id),
        job_dir=str(JobPaths.for_id(str(job.id)).root),
        ripped_files=getattr(job, "ripped_files", None),
        post_paths=getattr(job, "post_paths", None)
    )


# NOTE: the HTTP rip-progress endpoint + its rate-limit globals were
# removed in the #365 cleanup once all worker callbacks went in-process.
# The worker now calls workers.tasks._post_rip_progress which writes
# state directly via apply_job_state with the worker's own
# client-side throttle. See PR #433 and docs/ADR-001-postprocess-collapse.md.


class PostprocessProgressRequest(BaseModel):
    """Body for POST /jobs/{job_id}/postprocess-progress (callback from worker)."""
    post_progress: Optional[int] = None


_postprocess_progress_last_accept: Dict[str, float] = {}
POSTPROCESS_PROGRESS_RATE_LIMIT_SECONDS = 2


@router.post("/{job_id}/postprocess-progress")
def postprocess_progress_callback(
    job_id: str,
    body: PostprocessProgressRequest,
    db: Session = Depends(get_db),
    client_host: str = Depends(get_callback_client_host),
):
    """
    Callback for resume_postprocess worker: report post-processing progress. Localhost-only.
    Rate-limited to one request per job per 2 seconds.
    """
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(403, detail="Callback endpoints are localhost-only")
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    # #365 — derived, not column.
    post_state = job.derived_post_state
    if post_state not in ("running", "pending", "ready"):
        raise HTTPException(409, detail=f"Job post_state must be running (current: {post_state})")
    if body.post_progress is None:
        return {"ok": True}
    now = time.time()
    last = _postprocess_progress_last_accept.get(job_id, 0.0)
    if now - last < POSTPROCESS_PROGRESS_RATE_LIMIT_SECONDS:
        return {"ok": True, "throttled": True}
    _postprocess_progress_last_accept[job_id] = now
    updates = {"post_progress": max(0, min(100, body.post_progress))}
    apply_job_state(db, job, updates=updates, reason=None)
    try:
        from core.progress_emitter import emit_job_progress_debounced
        progress_data = {
            "disc_id": str(job.disc_id) if getattr(job, "disc_id", None) else None,
            "rip_progress": getattr(job, "rip_progress", 0),
            "rip_phase": getattr(job, "rip_phase", None),
            "post_progress": body.post_progress,
            "transfer_progress": getattr(job, "transfer_progress", None),
        }
        emit_job_progress_debounced(job_id, progress_data)
    except Exception as exc:
        log.warning("Failed to emit progress after postprocess-progress for job %s: %s", job_id, exc)
    return {"ok": True}


# NOTE: the HTTP transfer-progress endpoint + rate-limit globals were
# removed in the #365 cleanup. Worker now calls
# workers.tasks._post_transfer_progress directly via apply_job_state
# with its own client-side throttle. See PR #430.


def _mark_path_a_skipped_siblings_as_ignore(
    job: Any, sr_state: dict, db: Session,
) -> int:
    """Mark every disc_titles row whose `index` is in the duplicate-segment-map
    group but NOT in this job's rip_set with type='ignore'. Those titles
    weren't ripped (selective-rip skipped them on purpose), so they have no
    output file to label and shouldn't show up on the labeling Titles step.

    Idempotent: titles already marked with a non-empty type are left alone
    so a re-run can't clobber a user-applied type. Returns the number of
    rows mutated.
    """
    group_indexes = sr_state.get("group_member_indexes")
    rip_set = getattr(job, "rip_set", None)
    disc_id = getattr(job, "disc_id", None)
    if not disc_id or not isinstance(group_indexes, list) or not isinstance(rip_set, list):
        return 0
    skipped = set(int(i) for i in group_indexes) - set(int(i) for i in rip_set)
    if not skipped:
        return 0
    # Pull the candidate titles in one round-trip and filter in Python — the
    # `index` column is mutable across rescans (per the model docstring) so
    # we still match by current `index` after taking the lock at SELECT time.
    rows = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .filter(db_models.DiscTitle.index.in_(skipped))
        .all()
    )
    mutated = 0
    for row in rows:
        existing = (row.type or "").strip().lower()
        if existing and existing != "ignore":
            # Respect user-applied types — only fill in blanks.
            continue
        if existing == "ignore":
            # Already ignored, but still upgrade the reason if needed —
            # 'path_a_decoy' is a stronger signal than NULL or
            # 'makemkv_msg3307' for these rows.
            if getattr(row, "obfuscation_reason", None) != "path_a_decoy":
                row.obfuscation_reason = "path_a_decoy"
                row.obfuscation_flag = True
            continue
        # Path A skipped this sibling — an automated, derived decision.
        # source='auto' keeps user_type NULL; the chip system treats this
        # as "system-ignored" and (by default) hides it without requiring
        # a confirm-review step, since the canonical match decisively
        # rules out every sibling in the group.
        from api.crud import set_title_type
        set_title_type(row, "ignore", source="auto")
        # HIGH-tier reason: Path A explicitly skipped this sibling, so we
        # know with certainty it's a decoy of the canonical playlist.
        row.obfuscation_reason = "path_a_decoy"
        row.obfuscation_flag = True
        mutated += 1
    return mutated


def _clear_path_a_canonical_obfuscation_flag(
    job: Any, sr_state: dict, db: Session,
) -> bool:
    """Clear `obfuscation_flag` on the disc_title row that Path A matched as
    the canonical playlist. MakeMKV sets this flag during the original scan
    from MSG:3307 bit 0x01000000 on every playlist it suspects of
    obfuscation — including the real canonical on heavily-obfuscated discs
    like Midway. Once Path A's segment-reorder workflow confirms a specific
    playlist IS the canonical, the "Likely decoy" hint becomes a false
    signal and must be cleared so the labeling UI doesn't contradict
    itself. Idempotent. Returns True if the row was found and changed.
    """
    matched_index = sr_state.get("matched_playlist_index")
    disc_id = getattr(job, "disc_id", None)
    if not disc_id or matched_index is None:
        return False
    try:
        matched_index_int = int(matched_index)
    except (TypeError, ValueError):
        return False
    row = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == disc_id)
        .filter(db_models.DiscTitle.index == matched_index_int)
        .first()
    )
    if row is None:
        return False
    changed = False
    if getattr(row, "obfuscation_flag", False):
        row.obfuscation_flag = False
        changed = True
    # Also clear the tier-aware reason — Path A just confirmed this is
    # the real canonical, which overrides every other signal we have
    # (including MakeMKV's per-title bit and any earlier dedupe label).
    if getattr(row, "obfuscation_reason", None) is not None:
        row.obfuscation_reason = None
        changed = True
    # The user picked this row via the exploratory-rip ordering. Record
    # the provenance on user_type so the chip system shows "User
    # selected" alongside any pre-existing DiscDB/MakeMKV value in
    # auto_type. Use the effective `type` as the value — that's what
    # the user implicitly endorsed by submitting the ordering.
    effective_type = row.user_type if row.user_type is not None else row.auto_type
    if effective_type and row.user_type != effective_type:
        from api.crud import set_title_type
        set_title_type(row, effective_type, source="user")
        changed = True
    return changed


def _maybe_advance_canonical_complete(job: Any, job_id: str, db: Session, branch: str) -> bool:
    """If this job's segment_reorder_state is canonical_ripping_pending, advance
    it to canonical_complete and broadcast the change so the frontend's
    pathAActive$ flips false and the workspace hands off to the regular
    labeling/postprocess UI.

    Called from both the success and the failure-heal branches of
    rip_verification_complete so the advance happens regardless of which
    code path advanced rip_state to completed. Returns True when the stage
    was advanced (caller can use this for additional broadcasts on miss).
    """
    sr_state = getattr(job, "segment_reorder_state", None) or {}
    if not isinstance(sr_state, dict) or sr_state.get("stage") != "canonical_ripping_pending":
        return False
    sr_state = dict(sr_state)
    sr_state["stage"] = "canonical_complete"
    job.segment_reorder_state = sr_state
    # Advance the workflow_step out of the exploratory_rip pill so the user
    # leaves the path-a-workspace and lands on the next labeling step. Hit
    # profile (rare for Path A) jumps straight to transfer; miss profile
    # goes to the boxset/release step that's the start of normal labeling.
    # #365 Phase 2 § 6.4 — workflow_step="postprocess" replaced with
    # "transfer" since the standalone postprocess step was collapsed
    # into transfer's "preparing" sub-phase.
    if getattr(job, "workflow_step", None) == "exploratory_rip":
        job.workflow_step = "transfer" if branch == "hit" else "boxset"
    # Auto-mark the duplicate-segment-map siblings the selective rip skipped
    # on purpose as type='ignore' so they never reach the labeling Titles
    # step. Without this the user would see ~21 untouched duplicates
    # alongside the canonical rip and have to hand-ignore each one. The
    # ignore set is (group_member_indexes − rip_set): every group member
    # that wasn't part of the canonical rip.
    skipped_count = _mark_path_a_skipped_siblings_as_ignore(job, sr_state, db)
    if skipped_count:
        log.info(
            "Path A: job %s auto-ignored %d unripped duplicate sibling(s)",
            job_id, skipped_count,
        )
    if _clear_path_a_canonical_obfuscation_flag(job, sr_state, db):
        log.info(
            "Path A: job %s cleared obfuscation_flag on matched canonical index %s",
            job_id, sr_state.get("matched_playlist_index"),
        )
    db.commit()
    log.info("Path A: job %s canonical rip complete; advanced stage", job_id)

    # Broadcast for the miss branch — the hit branch's StageState.postprocess_started
    # call will broadcast via apply_job_state, but on miss this is the only
    # state change and apply_job_state never sees it.
    if branch == "miss":
        try:
            import asyncio
            from api.routers.websockets import _emit_to_job_workflow

            async def _emit_path_a_advance():
                try:
                    await _emit_to_job_workflow(job_id, changed_fields=['jobStatus'])
                except Exception as exc:
                    log.warning(
                        "Path A miss: failed to broadcast canonical_complete for job=%s: %s",
                        job_id, exc,
                    )

            try:
                loop = asyncio.get_running_loop()
                asyncio.create_task(_emit_path_a_advance())
            except RuntimeError:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    asyncio.run_coroutine_threadsafe(_emit_path_a_advance(), _app_instance.state.event_loop)
        except Exception as exc:
            log.warning(
                "Path A miss: workflow broadcast scheduling failed for job=%s: %s",
                job_id, exc,
            )
    return True


def _maybe_dispatch_segment_previews(job: Any, job_id: str) -> bool:
    """If the job is in segment-reorder exploratory phase, dispatch the
    preview-generation task and return True (caller should short-circuit
    its normal postprocess/preview chain).

    Called from both the success and "rip-clearly-done failure-heal"
    paths of rip_verification_complete + rip_complete so that whichever
    branch advances `rip_state` to completed also triggers Path A
    forward.
    """
    sr_state = getattr(job, "segment_reorder_state", None) or {}
    if not isinstance(sr_state, dict):
        return False
    if sr_state.get("stage") != "exploratory_ripping":
        return False
    log.info(
        "Path A: job %s is in segment-reorder exploratory phase; "
        "dispatching generate_segment_previews",
        job_id,
    )
    from workers.tasks import generate_segment_previews
    generate_segment_previews.delay(job_id)
    return True


class RipCompleteRequest(BaseModel):
    """Body for POST /jobs/{job_id}/rip-complete (callback from worker)."""
    success: bool
    ripped_files: Optional[Dict[str, str]] = None  # Optional; API uses existing job.ripped_files when omitted
    source_hashes: Optional[Dict[str, str]] = None
    error_reason: Optional[str] = None
    error_type: Optional[str] = None  # e.g. "disc_read" for critical user notification
    debug: Optional[Dict[str, Any]] = None  # Worker diagnostics (e.g. disc lock snapshot for drive_busy)
    # When True with success=False: allow stuck-recovery (lost success callback) if job looks copy-complete.
    # Never set for normal worker failures (e.g. partial MakeMKV copy with some titles failed).
    heal_stuck_copy: bool = False

    @model_validator(mode="after")
    def check_required_fields(self):
        if not self.success and not self.error_reason:
            raise ValueError("error_reason required when success is False")
        return self


def rip_complete_callback(
    job_id: str,
    body: RipCompleteRequest,
    db: Session,
    client_host: str = "127.0.0.1",
):
    """Copy-boundary callback from ``rip_disc``: success means MakeMKV copy
    finished; sets ``rip_phase=verification`` and enqueues
    ``rip_verification``. Final rip completion is applied by the
    rip-verification-complete callback.

    No longer a FastAPI route (#365 cleanup) — the HTTP endpoint was
    removed once the worker callback at
    ``workers.tasks._post_rip_complete_callback`` started invoking this
    function directly via Python import (PR #432). The ``client_host``
    parameter is kept with a localhost default for backward
    compatibility with the in-process call site; the actual localhost
    guard is now meaningless (the function is never reachable via the
    network) and the check below short-circuits.
    """
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        # Unreachable from in-process callers; kept as defensive
        # documentation of the historical localhost-only contract.
        raise HTTPException(403, detail="Callback endpoints are localhost-only")
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    rip_state = getattr(job, "rip_state", None)
    if body.success:
        if rip_state in ("completed", "skipped"):
            return {"ok": True}
        if rip_state != "running":
            raise HTTPException(409, detail=f"Job rip_state must be running to accept success (current: {rip_state})")
        phase = getattr(job, "rip_phase", None)
        if phase == "verification":
            log.info("rip_complete duplicate job_id=%s (already in verification)", job_id)
            return {"ok": True}
        if phase not in (None, "copy"):
            raise HTTPException(
                409,
                detail=f"rip-complete out of order: expected rip_phase copy or null (current: {phase})",
            )
        StageState.rip_copy_complete(db, job, reason="rip_complete copy ack")
        enqueue_rip_verification_for_job(str(job.id), reason="rip_complete copy ack")
        return {"ok": True}
    # Failure path
    rip_progress = getattr(job, "rip_progress", None) or 0
    ripped_files_existing = getattr(job, "ripped_files", None)
    has_ripped = bool(ripped_files_existing) and len(ripped_files_existing) > 0
    rip_clearly_done = rip_state in ("completed", "skipped") or (
        body.heal_stuck_copy
        and rip_progress >= 100
        and has_ripped
    )
    if rip_clearly_done:
        # Rip actually completed (e.g. success callback was lost; this may be a duplicate task's failure).
        # Transition to completed so the job is not stuck in running.
        if rip_state != "completed" and rip_state != "skipped":
            log.info(
                "Rip-complete failure for job %s but rip clearly done (heal_stuck_copy=%s, progress=%s, ripped_files=%s); applying rip_complete",
                job_id,
                body.heal_stuck_copy,
                rip_progress,
                len(ripped_files_existing or {}),
            )
            from core.job_state import _infer_profile
            branch = _infer_profile(job)
            if branch not in ("hit", "miss"):
                branch = "hit"
            ripped_files = getattr(job, "ripped_files", None) or {}
            StageState.rip_complete(
                db, job,
                branch=branch,
                ripped_files=ripped_files,
                source_hashes=(getattr(job, "disc_payload", None) or {}).get("source_hashes"),
                reason="rip_complete callback (failure ignored, rip clearly done)",
            )
            # Path A intercept on the failure-heal branch.
            if _maybe_dispatch_segment_previews(job, job_id):
                return {"ok": True, "segment_reorder_dispatched": True}
            if branch == "hit":
                # Phase 2 collapse (#365): same retargeting as the
                # rip-verification-complete handler.
                from workers.tasks import start_transfer as start_transfer_task
                start_transfer_task.delay(job_id)
                StageState.postprocess_started(db, job, reason="rip_complete (heal) enqueued start_transfer")
        return {"ok": True}
    if body.error_type == "drive_busy" or (
        body.error_reason and "Drive busy" in body.error_reason
    ):
        log.warning(
            "rip_complete_callback: drive_busy job_id=%s disc_num=%s mount_point=%s "
            "job_celery_task_id=%s error_type=%s debug=%s error_reason=%s",
            job_id,
            getattr(job, "disc_num", None),
            getattr(job, "mount_point", None),
            getattr(job, "celery_task_id", None),
            body.error_type,
            json.dumps(body.debug, default=str) if body.debug else None,
            (body.error_reason or "")[:2000],
        )
    StageState.rip_failed(
        db, job,
        error_reason=body.error_reason or "Rip failed",
        reason="rip_complete callback (failure)",
        error_type=body.error_type,
    )
    return {"ok": True}


class RipVerificationCompleteRequest(BaseModel):
    """Body for POST /jobs/{job_id}/rip-verification-complete (rip_verification worker)."""
    success: bool
    ripped_files: Optional[Dict[str, str]] = None
    source_hashes: Optional[Dict[str, str]] = None
    preview_detect_keys: Optional[List[str]] = None
    preview_detect_overrides: Optional[Dict[str, str]] = None
    error_reason: Optional[str] = None
    error_type: Optional[str] = None

    @model_validator(mode="after")
    def check_required_fields(self):
        if self.success and not (self.ripped_files and len(self.ripped_files) > 0):
            raise ValueError("ripped_files required when success is True")
        if not self.success and not self.error_reason:
            raise ValueError("error_reason required when success is False")
        return self


def rip_verification_complete_callback(
    job_id: str,
    body: RipVerificationCompleteRequest,
    db: Session,
    client_host: str = "127.0.0.1",
):
    """Callback after hashing/mapping MKVs; applies ``StageState.rip_complete``
    and enqueues ``start_transfer`` (hit) or ``preview_raw_titles`` (miss).

    No longer a FastAPI route (#365 cleanup) — the HTTP endpoint was
    removed once the worker callback at
    ``workers.tasks._post_rip_verification_complete_callback`` started
    invoking this function directly via Python import (PR #431). The
    ``client_host`` parameter is kept with a localhost default for
    backward compatibility with the in-process call site; the actual
    localhost guard is now meaningless and the check below
    short-circuits.
    """
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        # Unreachable from in-process callers; kept as defensive
        # documentation of the historical localhost-only contract.
        raise HTTPException(403, detail="Callback endpoints are localhost-only")
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    rip_state = getattr(job, "rip_state", None)
    if rip_state in ("completed", "skipped"):
        return {"ok": True}
    if body.success:
        if rip_state != "running":
            raise HTTPException(409, detail=f"Job rip_state must be running (current: {rip_state})")
        phase = getattr(job, "rip_phase", None)
        if phase not in (None, "verification"):
            raise HTTPException(
                409,
                detail=f"rip-verification-complete out of order (rip_phase={phase})",
            )
        from core.job_state import _infer_profile
        branch = _infer_profile(job)
        if branch not in ("hit", "miss"):
            branch = "hit"
        ripped_files = body.ripped_files or {}

        # #348: Validate that important (non-ignore) titles have ripped files.
        # If any required title is missing from ripped_files, fail the rip instead of proceeding.
        # Selective-rip exception: when job.rip_set is set, by design we
        # only ripped a subset of titles (the canonical + non-mass), so the
        # "missing important titles" check is meaningless and would always
        # trigger. Skip the check on Path A jobs.
        disc_id = getattr(job, "disc_id", None)
        is_selective_rip = bool(getattr(job, "rip_set", None))
        if disc_id and ripped_files and not is_selective_rip:
            try:
                important_titles = (
                    db.query(db_models.DiscTitle)
                    .filter(
                        db_models.DiscTitle.disc_id == disc_id,
                        or_(
                            db_models.DiscTitle.type.is_(None),
                            db_models.DiscTitle.type == "",
                            ~db_models.DiscTitle.type.ilike("ignore"),
                        ),
                    )
                    .all()
                )
                if important_titles:
                    ripped_ids = set(str(k) for k in ripped_files.keys())
                    missing_important = [
                        t for t in important_titles
                        if str(t.id) not in ripped_ids
                    ]
                    if missing_important:
                        missing_names = ", ".join(
                            (t.title or t.source_file or str(t.id)[:8]) for t in missing_important[:5]
                        )
                        error_msg = (
                            f"{len(missing_important)} important title(s) missing from rip output: {missing_names}. "
                            "The disc may have read errors. Try reinserting or cleaning the disc."
                        )
                        log.warning("Job %s: rip verification found missing important titles: %s", job_id, error_msg)
                        StageState.rip_failed(
                            db, job,
                            error_reason=error_msg,
                            error_type="missing_titles",
                            reason="rip verification: important titles missing from output",
                        )
                        return {"ok": True, "failed": True, "reason": error_msg}
            except Exception as val_exc:
                log.warning("Job %s: title validation failed (non-blocking): %s", job_id, val_exc)

        StageState.rip_complete(
            db,
            job,
            branch=branch,
            ripped_files=ripped_files,
            source_hashes=body.source_hashes,
            reason="rip_verification_complete callback",
        )

        # Path A intercept: if this was an exploratory rip for segment-reorder,
        # skip the normal postprocess/preview-detect chain and dispatch
        # generate_segment_previews instead. The user takes over from there.
        if _maybe_dispatch_segment_previews(job, job_id):
            return {"ok": True, "segment_reorder_dispatched": True}

        # Path A canonical rip just completed — advance stage off
        # canonical_ripping_pending so the workspace component stops rendering
        # and the normal labeling/postprocess UI takes over.
        _maybe_advance_canonical_complete(job, job_id, db, branch)

        if branch == "hit":
            # Phase 2 collapse (#365): the hit branch now enqueues the unified
            # start_transfer worker, which sets transfer_phase=preparing and
            # delegates to the existing prep body. resume_postprocess still
            # exists; it becomes a forwarding shim in commit 3 for any in-flight
            # jobs that were queued under the old task name pre-deploy.
            from workers.tasks import start_transfer as start_transfer_task
            start_transfer_task.delay(job_id)
            StageState.postprocess_started(db, job, reason="rip_verification_complete enqueued start_transfer")
        elif branch == "miss" and body.preview_detect_keys:
            preview_raw_titles.delay(
                job_id,
                body.preview_detect_keys,
                rel_path_overrides=body.preview_detect_overrides,
            )
        return {"ok": True}
    # failure
    rip_progress = getattr(job, "rip_progress", None) or 0
    ripped_files_existing = getattr(job, "ripped_files", None)
    rip_clearly_done = (
        rip_state in ("completed", "skipped")
        or rip_progress >= 100
        or (bool(ripped_files_existing) and len(ripped_files_existing) > 0)
    )
    if rip_clearly_done and rip_state != "completed" and rip_state != "skipped":
        log.info(
            "Rip-verification failure for job %s but rip clearly done; applying rip_complete",
            job_id,
        )
        from core.job_state import _infer_profile
        branch = _infer_profile(job)
        if branch not in ("hit", "miss"):
            branch = "hit"
        ripped_files = getattr(job, "ripped_files", None) or {}
        StageState.rip_complete(
            db,
            job,
            branch=branch,
            ripped_files=ripped_files,
            source_hashes=(getattr(job, "disc_payload", None) or {}).get("source_hashes"),
            reason="rip_verification_complete (failure ignored, rip clearly done)",
        )
        # Path A: same intercept as the success path. The "failure" here
        # is often a verification false-positive (e.g. selective rip
        # tripping the missing-titles check on disc_titles it didn't
        # rip on purpose), and the rip itself is clearly fine.
        if _maybe_dispatch_segment_previews(job, job_id):
            return {"ok": True, "segment_reorder_dispatched": True}
        # Same canonical_complete advance as the success path, so jobs that
        # came through this heal branch don't get stuck on canonical_ripping_pending
        # with rip_state already completed.
        _maybe_advance_canonical_complete(job, job_id, db, branch)
        if branch == "hit":
            # Phase 2 collapse (#365): mirror the success-path retargeting.
            from workers.tasks import start_transfer as start_transfer_task
            start_transfer_task.delay(job_id)
            StageState.postprocess_started(db, job, reason="rip_verification_complete (heal) enqueued start_transfer")
        elif branch == "miss" and body.preview_detect_keys:
            preview_raw_titles.delay(
                job_id,
                body.preview_detect_keys,
                rel_path_overrides=body.preview_detect_overrides,
            )
        return {"ok": True}
    StageState.rip_failed(
        db,
        job,
        error_reason=body.error_reason or "Rip verification failed",
        reason="rip_verification_complete callback (failure)",
        error_type=body.error_type,
    )
    return {"ok": True}


# NOTE: the HTTP postprocess-complete + transfer-complete callback
# endpoints were removed in the #365 cleanup. Workers now call
# StageState directly via workers.tasks._post_postprocess_complete_callback
# (PR #427) and the in-API _complete_transfer / _fail_transfer helpers
# via workers.tasks._post_transfer_complete_callback (PR #429).


@router.get("", response_model=List[JobListItem])
def list_jobs(limit: int = 50, db: Session = Depends(get_db)):
    try:
        # _cleanup_stale_jobs removed from request path — now runs as periodic
        # background task (every 60s in lifespan). Previously held DB session
        # for 2-3s while doing Celery inspect() + Redis AsyncResult checks.
        # Eagerly load disc, release, and movie relationships to ensure correct movie_name
        q = (
            db.query(crud.models.Job)  # type: ignore[attr-defined]
            .options(
                joinedload(crud.models.Job.disc)  # type: ignore[attr-defined]
                .joinedload(db_models.Disc.release)  # type: ignore[attr-defined]
                .joinedload(db_models.Release.movie)  # type: ignore[attr-defined]
            )
            .order_by(crud.models.Job.created_at.desc())  # type: ignore[attr-defined]
        )
        jobs = q.limit(max(1, min(limit, 200))).all()
    except ProgrammingError as e:
        orig = getattr(e, "orig", None)
        if orig is not None and getattr(orig, "pgcode", None) == "42P01":  # undefined_table
            log.warning("list_jobs: jobs table not available (schema not ready?): %s", e)
            return []
        raise
    items: List[JobListItem] = []
    for job in jobs:
        pipeline, phase = _derive_pipeline(job)
        payload: dict[str, Any] = {}
        disc = getattr(job, "disc", None)
        rel = getattr(disc, "release", None) if disc else None
        if disc:
            payload["disc_hash"] = disc.content_hash
            payload["disc_slug"] = disc.disc_slug
            payload["disc_name"] = disc.disc_name
            payload["disc_number"] = disc.disc_number
            payload["disc_format"] = disc.format
        if rel:
            payload["disc_group"] = rel.slug
            payload["release_id"] = str(rel.id)
            payload["release_slug"] = rel.slug
            payload["group_type"] = rel.type
            payload["release_name"] = rel.name
            # Include movie_name from linked movie if available
            if rel.movie:
                payload["movie_name"] = rel.movie.name
            # Get release_year from release (or boxset if available)
            if hasattr(rel, "boxset") and rel.boxset:
                payload["release_year"] = rel.boxset.year
            else:
                payload["release_year"] = getattr(rel, "release_year", None)
            # Get resolution from release record
            payload["resolution"] = getattr(rel, "resolution", None)
        else:
            # Unlinked disc: use job.disc_payload so frontend can match job to release by slug
            dp = getattr(job, "disc_payload", None) or {}
            payload["release_id"] = str(dp["release_id"]) if dp.get("release_id") else None
            payload["release_slug"] = dp.get("release_slug") or dp.get("disc_group")
            if payload.get("release_slug"):
                payload["disc_group"] = payload["release_slug"]
        items.append(JobListItem(
            jobId=str(job.id),
            disc_num=job.disc_num,
            mount_point=job.mount_point,
            disc_hash=payload.get("disc_hash"),
            job_status=job.job_status,
            scan_state=getattr(job, "scan_state", None),
            mode=job.mode,
            rip_progress=job.rip_progress,
            rip_phase=getattr(job, "rip_phase", None),
            post_progress=getattr(job, "post_progress", 0),
            created_at=job.created_at,
            updated_at=job.updated_at,
            job_dir=str(JobPaths.for_id(str(job.id)).root),
            show_title=payload.get("show_title"),
            movie_name=payload.get("movie_name"),
            disc_group=payload.get("disc_group"),
            release_id=payload.get("release_id"),
            release_slug=payload.get("release_slug"),
            transfer_progress=getattr(job, "transfer_progress", None),
            pipeline=pipeline,
            phase=phase,
            discdb_hit=_workflow_discdb_hit_for_context(job),
            titles_completed=getattr(job, "titles_completed", None),
            total_titles=getattr(job, "total_titles", None),
            per_title_progress=getattr(job, "per_title_progress", None),
            dev_mode=getattr(job, "dev_mode", None),
            dev_validation=getattr(job, "dev_validation", None),
            export_path=getattr(job, "export_path", None),
            resolution=payload.get("resolution"),
            release_year=payload.get("release_year"),
        ))
    return items


@router.get("/{job_id}/dev-report")
def get_dev_report(job_id: str, db: Session = Depends(get_db)):
    """
    Serve the dev-mode validation report HTML for a job.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    report_path = None
    dev_val = getattr(job, "dev_validation", None)
    if isinstance(dev_val, dict):
        report_path = dev_val.get("report_path")
    if not report_path:
        fin = getattr(getattr(job, "disc", None), "finalize_result", None)
        if isinstance(fin, dict):
            dv = fin.get("dev_validation")
            if isinstance(dv, dict):
                report_path = dv.get("report_path")
    if not report_path:
        raise HTTPException(404, detail="No dev-mode report for this job")

    try:
        resolved = Path(report_path).expanduser().resolve()
    except Exception as exc:
        raise HTTPException(400, detail=f"Invalid report path: {exc}") from exc

    export_root = get_export_root().resolve()
    if export_root not in resolved.parents and resolved != export_root:
        raise HTTPException(400, detail="Report path is outside export root")
    if not resolved.exists():
        raise HTTPException(404, detail="Report not found on disk")

    return FileResponse(resolved, filename=resolved.name, media_type="text/html")


def _build_job_metadata(job: db_models.Job) -> Dict[str, Any]:
    """Build metadata dictionary for path template resolution."""
    disc = getattr(job, "disc", None)
    release = getattr(disc, "release", None) if disc else None
    movie = getattr(release, "movie", None) if release else None
    
    metadata = {}
    
    # Movie information
    if movie:
        metadata["movie_name"] = movie.name
        metadata["movie_year"] = movie.production_year
        metadata["year"] = movie.production_year  # Alias
    
    # Release information
    if release:
        metadata["release_name"] = release.name
        metadata["release_year"] = release.release_year
        metadata["release_slug"] = release.slug
    
    # Disc information
    if disc:
        metadata["disc_number"] = disc.disc_number
        metadata["disc_name"] = disc.disc_name
        metadata["format"] = disc.format
        metadata["type"] = release.type if release else (disc.format or "movie")
    
    return metadata


def _verify_transfer_destination(
    dest_paths: list[str], job: db_models.Job, db: Session
) -> tuple[bool, Optional[str]]:
    """
    Verify destination before marking transfer complete.
    Returns (True, None) if safe to complete, (False, error_message) if we must fail and notify.
    - Requires non-empty, non-blank dest_paths.
    - For local paths: requires existence and validate_transfer_output (existence + size/hash) to pass.
    - For remote URIs (smb://, nfs://, etc.): only requires non-empty path (cannot verify from this process).
    """
    if not dest_paths or not any((p or "").strip() for p in dest_paths):
        return False, "Transfer reported success but no destination path"
    first = (dest_paths[0] or "").strip()
    if not first:
        return False, "Transfer reported success but no destination path"
    # Skip file-level verification for remote URIs (cannot check from this process)
    if first.startswith("smb://") or first.startswith("nfs://") or first.startswith("rsync://") or "://" in first.split("/")[0]:
        return True, None
    dest_path = Path(first)
    if not dest_path.exists():
        return False, f"Destination path not found: {dest_path}"
    try:
        from core.stage_validation import validate_transfer_output
        validation_result = validate_transfer_output(job, db, dest_path)
        if not validation_result.valid:
            return False, "Transfer destination verification failed: " + "; ".join(validation_result.errors)
    except Exception as exc:
        return False, f"Transfer destination verification error: {exc}"
    return True, None


def _update_title_file_paths_after_transfer(job: db_models.Job, db: Session, dest_paths: list[str]) -> None:
    """#607: Map transferred files back to title_ids and update
    ``DiscTitle.file_path`` / ``file_path_stage``.

    Pre-#607 the writer walked the destination via
    ``Path(dest_root).rglob('*.mkv')``. That only works on a local
    filesystem — for SMB / rsync / NFS the ``dest_path`` returned by
    the protocol is a URI string (``smb://host/share/...``,
    ``user@host:/path/...``) that ``pathlib.Path`` can't walk, so the
    title map stayed empty and the rows never advanced past
    ``file_path_stage='postprocess'``. The Library disc drawer then
    rendered "In transient" for files that had already been
    transferred.

    The fix constructs the per-title destination URI/path
    deterministically from ``Job.post_paths`` (which already carries
    ``{title_id → relative_path}`` from the postprocess stage) joined
    to ``dest_root``. No filesystem stat'ing required — the same path
    that the transfer protocol wrote the bytes to is the path we
    record.

    Handles both directory transfers (``dest_root`` is a folder, each
    title's rel_path appended) and single-file transfers (``dest_root``
    is the final ``.mkv`` itself, matched by basename against
    post_paths).
    """
    disc_id = getattr(job.disc, "id", None) if getattr(job, "disc", None) else None
    post_paths = getattr(job, "post_paths", None) or {}
    if not disc_id or not post_paths or not dest_paths:
        return
    try:
        dest_root = dest_paths[0] if dest_paths else None
        if not dest_root:
            return
        title_dest_map: dict[str, str] = {}
        # Single-file transfer: dest_root IS the final file. Match by
        # basename to the title whose post_paths entry has the same
        # filename. (Path() splits remote URIs the same way as POSIX
        # paths for the trailing component, so rsplit is mode-agnostic.)
        if dest_root.endswith(".mkv"):
            fname = dest_root.rsplit("/", 1)[-1]
            for tid, rel in post_paths.items():
                if rel and rel.rsplit("/", 1)[-1] == fname:
                    title_dest_map[tid] = dest_root
                    break
        else:
            # Directory transfer: concatenate dest_root + per-title
            # rel_path. dest_root may or may not have a trailing slash
            # (depends on the protocol's return shape); normalise so the
            # join is consistent regardless of mode (file:, smb:,
            # user@host:, …).
            base = dest_root if dest_root.endswith("/") else dest_root + "/"
            for tid, rel in post_paths.items():
                if not rel:
                    continue
                title_dest_map[tid] = base + rel.lstrip("/")
        if title_dest_map:
            titles = db.query(db_models.DiscTitle).filter(
                db_models.DiscTitle.disc_id == disc_id,
                db_models.DiscTitle.id.in_(list(title_dest_map.keys())),
            ).all()
            for t in titles:
                if t.id in title_dest_map:
                    t.file_path = title_dest_map[t.id]
                    t.file_path_stage = "transfer"
            # #634: ``StageState.transfer_complete`` (called just above in
            # ``_complete_transfer``) already committed the outer state
            # transition. If we only ``flush()`` here, the writer's changes
            # sit in the session and are dropped when the session closes,
            # leaving disc_titles at ``file_path_stage='postprocess'`` even
            # though ``transfer_state='completed'`` — exactly the "IN
            # TRANSIENT after successful SMB transfer" symptom.
            db.commit()
    except Exception as exc:
        log.warning("_update_title_file_paths_after_transfer: %s", exc)


def _complete_transfer(job: db_models.Job, db: Session, dest_paths: list[str], job_metadata: Dict[str, Any]) -> None:
    """Complete a successful transfer and update job state."""
    profile = job.stage_profile or "miss"
    next_job_status = job.job_status
    next_phase = "transfer"
    next_finalize_release_state = job.finalize_release_state
    
    if profile == "hit" or job.finalize_release_state in ("completed", "skipped"):
        next_job_status = "completed"
        next_phase = "complete"
        next_finalize_release_state = next_finalize_release_state or "skipped"
    elif profile == "miss":
        next_phase = "complete"  # Skip finalize_release phase, go directly to complete

    # Sanitize in-flight job_status across transfer success: "validating" was set by
    # the postprocess worker and must not survive into a stage where startup recovery
    # would treat it as needing local-transient reconciliation. We cannot force
    # "completed" for miss profile here (label/finalize/finalize_release may still be
    # pending), but "running" is always safe and matches the actual job lifecycle.
    # Root cause of #366 leak.
    if next_job_status == "validating":
        next_job_status = "running"
    
    completion_stage_updates: dict[str, Any] = {}
    if next_job_status == "completed":
        # #365 step 3d — no more post_state column write. The validation
        # in _validate_completed_invariant reads derived_post_state which
        # returns "completed" once transfer_state="completed" is set in
        # this same update (step 4 of the derivation table).
        completion_stage_updates = {
            "rip_state": getattr(job, "rip_state", None) or ("completed" if (job.rip_progress or 0) >= 100 else None),
            "transfer_state": "completed",
            "label_state": getattr(job, "label_state", None) or ("skipped" if (profile or "").lower() == "hit" else None),
            "finalize_state": getattr(job, "finalize_state", None) or ("skipped" if (profile or "").lower() == "hit" else None),
        }

    StageState.transfer_complete(
        db,
        job,
        dest_paths=dest_paths,
        reason="transfer completed",
        transfer_progress=100,
        transfer_error=None,
        job_status=next_job_status,
        phase=next_phase,
        finalize_release_state=next_finalize_release_state,
        **{k: v for k, v in completion_stage_updates.items() if v is not None},
    )
    # Update file_path on DiscTitle rows to final transfer destination paths
    _update_title_file_paths_after_transfer(job, db, dest_paths)
    # Emit disc context update after job status change
    try:
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_disc_context_when_job_updates(str(job.id), db))
        except RuntimeError:
            from api.main import _app_instance
            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                loop = _app_instance.state.event_loop
                asyncio.run_coroutine_threadsafe(_emit_disc_context_when_job_updates(str(job.id), db), loop)
    except Exception as exc:
        log.warning(f"Failed to emit disc context update for job {job.id}: {exc}")
    
    # Check storage after transfer completes
    try:
        from core.transfer.service import get_active_config, check_storage
        active_config = get_active_config(db)
        if active_config and active_config.mode in ("smb", "nfs", "rsync"):
            storage_info, error = check_storage(db, active_config)
            if error:
                logging.warning(f"[{job.id}] Could not check storage after transfer: {error}")
            elif storage_info:
                free_gb = storage_info.get("free", 0) / (1024 ** 3)
                logging.info(f"[{job.id}] Transfer destination storage after transfer: {free_gb:.2f} GB free")
    except Exception as e:
        logging.warning(f"[{job.id}] Failed to check storage after transfer: {e}")


def _fail_transfer(job: db_models.Job, db: Session, error_msg: str, dest_paths: list[str] = None) -> None:
    """Handle a failed transfer. Does NOT fail the overall job — only transfer_state.

    The job stays running so the user can fix the destination and retry.
    """
    try:
        StageState.transfer_failed(
            db,
            job,
            error_reason=error_msg,
            dest_paths=dest_paths,
            reason="transfer failed",
        )
    except Exception:
        job.transfer_state = "failed"
        job.transfer_error = error_msg

    # Clean up partially transferred files
    if dest_paths:
        for d in dest_paths:
            try:
                d_path = Path(d)
                if d_path.exists():
                    if d_path.is_dir():
                        shutil.rmtree(d_path, ignore_errors=True)
                    else:
                        d_path.unlink()
            except Exception:
                pass
    
    db.commit()
    db.refresh(job)


def _try_src_equals_dest_shortcut(
    job: db_models.Job,
    db: Session,
    src_root: Path,
    config: db_models.TransferConfig,
    job_metadata: Dict[str, Any],
) -> bool:
    """#365 step 5b'b — src==dest short-circuit.

    Under ``MKVAUTO_RENAME_DIRECT_TO_DEST=1`` + local mode,
    :func:`workers.tasks._resolve_transfer_src_root` returns
    ``config.transfer_dir`` (where rename already wrote). The downstream
    copy paths in :func:`transfer_job` (library_dirs / use_final_map /
    regular) all assume ``src_root != dest_root``:

      * library_dirs only works by accident (``if dst_file.exists(): continue``)
      * use_final_map crashes — ``shutil.copy2(src, dest)`` with same
        path raises :class:`shutil.SameFileError`
      * regular self-nests via ``transfer_dir / src_path.name``

    So when src==dest we must skip the copy entirely. Identification of
    "this rip's files at the destination" uses Matroska Segment UID
    (#448 / PR #451) — robust to any rename or library reorganisation
    between postprocess and transfer.

    Returns ``True`` when the shortcut applied and the transfer is
    complete (caller should ``return get_status(...)``). Returns
    ``False`` when the shortcut is not applicable (caller continues
    with the normal scenarios). Raises :class:`HTTPException` 500 when
    the shortcut is applicable but a hard precondition is violated
    (pre-#448 legacy job with no captured UIDs, or file count mismatch
    at the destination) — fail loud rather than fall through to the
    broken copy paths.
    """
    if config.mode != "local":
        return False
    transfer_dir = (getattr(config, "transfer_dir", None) or "").strip()
    if not transfer_dir:
        return False
    resolved_dest_root = Path(transfer_dir).resolve()
    if src_root != resolved_dest_root:
        return False

    # Shortcut is applicable. From here we either complete the transfer
    # or fail loud — falling through to the existing scenarios is not
    # safe (see docstring).
    from core.mkv_identity import read_segment_uid

    titles = (getattr(job.disc, "titles", None) or []) if getattr(job, "disc", None) else []
    expected_uids = {t.segment_uid: t for t in titles if getattr(t, "segment_uid", None)}
    if not expected_uids:
        msg = (
            "Transfer src==dest but no segment_uids captured. This is a "
            "pre-#451 legacy job. Either re-rip the disc so postprocess "
            "captures segment_uids, or backfill them via "
            "`mkvmerge -J <file>` for each row in disc_titles."
        )
        _fail_transfer(job, db, "src==dest shortcut requires segment_uids (pre-#451 legacy job)")
        raise HTTPException(500, detail=msg)

    # Walk only this rip's post_paths — O(rip_size), not O(library_size).
    dest_files: list[Path] = []
    matched_uids: set[str] = set()
    for rel in (getattr(job, "post_paths", None) or {}).values():
        p = (src_root / rel).resolve()
        if not p.exists():
            continue
        uid = read_segment_uid(str(p))
        if uid and uid in expected_uids:
            dest_files.append(p)
            matched_uids.add(uid)

    if len(matched_uids) != len(expected_uids):
        msg = (
            f"Transfer src==dest but file count mismatch: "
            f"found {len(matched_uids)}/{len(expected_uids)} expected segment_uids at dest"
        )
        _fail_transfer(job, db, f"src==dest: {msg}")
        raise HTTPException(500, detail=msg)

    _advance_transfer_phase(
        db, job, "verifying",
        reason="src==dest: skipping copy, files already at dest",
    )
    _complete_transfer(job, db, [str(p) for p in dest_files], job_metadata)
    return True


def _execute_local_transfer_use_final_map(
    db: Session,
    job: db_models.Job,
    src_root: Path,
    config: db_models.TransferConfig,
    output_files: Optional[Dict[str, str]],
    job_metadata: Dict[str, Any],
    *,
    transfer_progress_callback: Callable[[int], None],
    hash_progress_callback: Callable[[int, str], None],
) -> list[str]:
    """Execute the use_final_map local-mode transfer scenario (#365 § 6.1).

    Copies the selective files in ``job.post_paths`` / ``job.ripped_files``
    (or ``output_files`` override) from ``src_root`` into
    ``config.transfer_dir``, preserving the relative path structure.
    Single-segment paths get ``Movies/`` or ``Series/`` prepended when
    ``src_root`` already has those library-style top dirs — matches the
    legacy inline endpoint behaviour exactly.

    Used by:
      * ``transfer_job`` HTTP endpoint when ``use_final_map`` is set and
        ``config.mode == "local"``.
      * (Phase 2 § 6.1 follow-up) the ``start_transfer`` worker after
        prep, to auto-progress local-mode jobs without a manual click.

    Returns the list of top-level destination directories (used by
    ``_verify_transfer_destination`` and ``_complete_transfer``).

    Raises ``HTTPException`` for hard failures (not enough free space,
    setup error, transfer error, destination verification failure) —
    same status codes the inline implementation used so callers see
    no behaviour change.
    """
    dest_root = Path(config.transfer_dir).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)

    have_movies = (src_root / "Movies").exists()
    have_series = (src_root / "Series").exists()
    # Use post_paths (preferred) or ripped_files (fallback) or output_files override.
    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    file_paths = post_paths if post_paths else ripped_files
    rel_iter: list[str] = list(
        (output_files or {}).values() if output_files else file_paths.values()
    )

    def _normalise(rel: str) -> Path:
        rel_path = Path(rel)
        if len(rel_path.parts) == 1:
            if have_movies:
                return Path("Movies") / rel_path
            if have_series:
                return Path("Series") / rel_path
        return rel_path

    # --- Phase 1: setup + space check ----------------------------------
    dests_fp: list[str] = []
    try:
        total_bytes = 0
        for rel in rel_iter:
            src = (src_root / _normalise(rel)).resolve()
            try:
                if src.exists():
                    total_bytes += src.stat().st_size
            except FileNotFoundError:
                continue

        try:
            usage = shutil.disk_usage(dest_root)
            if usage.free < total_bytes:
                raise HTTPException(
                    400,
                    detail=f"Not enough free space in target {dest_root} "
                           f"(need {total_bytes} bytes, have {usage.free})",
                )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, detail=f"Failed to check free space: {exc}")

        copied_bytes = 0
    except HTTPException:
        raise
    except Exception as exc:
        _fail_transfer(job, db, str(exc), dests_fp)
        transfer_log.exception("Job %s: final_paths transfer failed (setup): %s", job.id, exc)
        raise HTTPException(500, detail=f"Transfer failed: {exc}")

    # --- Phase 2: copy + hash verify + complete -------------------------
    dests: list[str] = []
    try:
        for rel in rel_iter:
            rel_path = _normalise(rel)
            src = (src_root / rel_path).resolve()
            if not src.exists():
                continue

            dest_file = dest_root / rel_path
            dest_file.parent.mkdir(parents=True, exist_ok=True)

            shutil.copy2(src, dest_file)
            copied_bytes += src.stat().st_size

            # Transfer-step progress: 0-100% of the copy → 0-50% overall.
            transfer_pct = int(min(100, (copied_bytes / max(1, total_bytes)) * 100))
            overall_pct = int(transfer_pct * 50 / 100)
            transfer_progress_callback(overall_pct)

        top_levels = {Path(rel).parts[0] for rel in rel_iter if rel}
        dests = [str(dest_root / tl) for tl in sorted(top_levels)] if top_levels else [str(dest_root)]

        # --- Phase 3: hash verification (50-100% overall progress) ----
        dest_files: list[Path] = []
        for dest_dir in dests:
            dest_path = Path(dest_dir)
            if dest_path.exists():
                dest_files.extend(list(dest_path.rglob("*.mkv")))

        expected_hashes: Dict[str, str] = {}
        for rel in rel_iter:
            src = (src_root / _normalise(rel)).resolve()
            if src.exists() and src.is_file():
                try:
                    from core.transfer.validation import calculate_file_hash
                    expected_hashes[src.name] = calculate_file_hash(src)
                except Exception as e:
                    log.warning(f"[{job.id}] Could not calculate source hash for {src}: {e}")

        if dest_files and expected_hashes:
            from core.transfer.service import verify_transferred_files_batch
            verify_results = verify_transferred_files_batch(
                dest_files,
                expected_hashes,
                progress_cb=hash_progress_callback,
            )
            all_verified = all(v is True for v in verify_results.values() if v is not None)
            if all_verified:
                log.info(f"Job {job.id}: All hash verifications passed")
            elif any(v is False for v in verify_results.values()):
                log.warning(f"Job {job.id}: Some hash verifications failed")

        # --- Phase 4: destination verification + complete --------------
        ok, verify_error = _verify_transfer_destination(dests, job, db)
        if not ok:
            _fail_transfer(job, db, verify_error or "Transfer destination verification failed", dests)
            transfer_log.warning("Job %s: %s", job.id, verify_error)
            raise HTTPException(500, detail=verify_error or "Transfer failed")

        _complete_transfer(job, db, dests, job_metadata)
        transfer_log.info("Job %s: final_paths transfer completed -> %s", job.id, dests)
        return dests
    except HTTPException:
        raise
    except Exception as exc:
        _fail_transfer(job, db, str(exc), dests)
        transfer_log.exception("Job %s: final_paths transfer failed: %s", job.id, exc)
        raise HTTPException(500, detail=f"Transfer failed: {exc}")


def _transfer_failure_maybe_retry(
    session: Session,
    job_row: db_models.Job,
    error_msg: str,
    dest_paths: Optional[list[str]],
    history_id: Optional[Any],
    job_id_str: str,
) -> bool:
    """
    On transfer failure: try 1 automatic retry, then give up and wait for user.

    Transfer failure does NOT fail the overall job — only transfer_state is set
    to "failed". The job stays running so the user can fix the destination and
    retry without re-ripping.

    Returns True if caller should retry immediately, False if retries exhausted.
    """
    session.refresh(job_row)
    retry_count = (job_row.transfer_retry_count or 0) + 1
    # 1 automatic retry attempt; after that, wait for user to fix and retry manually
    max_auto_retries = 1
    if retry_count > max_auto_retries:
        _fail_transfer(job_row, session, error_msg, dest_paths)
        if history_id:
            try:
                from core.transfer.utils import history as transfer_history
                transfer_history.log_transfer_failed(session, history_id=history_id, error=error_msg)
            except Exception as hist_exc:
                log.warning(f"[{job_id_str}] Failed to log transfer failure: {hist_exc}")
        return False
    apply_job_state(
        session,
        job_row,
        updates={"transfer_state": "pending", "transfer_error": None, "transfer_retry_count": retry_count},
        reason="auto-retry",
    )
    session.commit()
    log.info("Job %s: transfer failure, auto-retrying (%s/%s): %s", job_id_str, retry_count, max_auto_retries, error_msg)
    return True


@router.post("/{job_id}/transfer/start")
def start_transfer_endpoint(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Single entry point for the collapsed rename + hash + transfer sequence (#365).

    Phase 2 scaffolding. Enqueues the unified ``start_transfer`` Celery task.
    During this commit the task delegates to ``resume_postprocess`` and the
    existing post-postprocess flow handles the rest; later commits expand it
    to drive the full sequence under one task. The endpoint is callable
    today but is not yet on the production auto-progression path — commit 2
    wires ``rip-verification-complete`` to it.

    Used by:
      * the auto-progression path from ``rip-verification-complete``
        (added in commit 2)
      * the frontend's manual Start Transfer / Retry Transfer button
        (re-targeted in commit 4)

    Preconditions:
      * ``rip_state == "completed"``
      * ``label_state in {"completed", "skipped", None}`` (None for legacy
        jobs predating the label stage)
      * ``transfer_state`` not currently ``"running"`` or ``"pending"``

    Returns ``{status: "queued", task_id: <celery_task_id>}`` on success.

    See ``docs/ADR-001-postprocess-collapse.md`` and
    ``docs/plans/postprocess-collapse-325-365.md``.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    rip_state = getattr(job, "rip_state", None)
    if rip_state != "completed":
        raise HTTPException(409, detail=f"Cannot start transfer: rip_state is {rip_state!r} (must be 'completed')")
    label_state = getattr(job, "label_state", None)
    if label_state not in ("completed", "skipped", None):
        raise HTTPException(409, detail=f"Cannot start transfer: label_state is {label_state!r} (must be 'completed' or 'skipped')")
    transfer_state = getattr(job, "transfer_state", None)
    if transfer_state in ("running", "pending"):
        raise HTTPException(409, detail=f"Transfer already in progress (transfer_state={transfer_state!r})")

    from workers.tasks import start_transfer as start_transfer_task
    task = start_transfer_task.delay(job_id)
    log.info("start_transfer_endpoint: enqueued start_transfer for job %s (task_id=%s)", job_id, task.id)
    return {"status": "queued", "task_id": task.id}


@router.post("/{job_id}/transfer", response_model=JobStatus)
def transfer_job(
    job_id: str,
    req: Optional[TransferRequest] = Body(default=None),
    db: Session = Depends(get_db),
    background_tasks: BackgroundTasks = None,
    target_dir: Optional[str] = None,
):
    if req is None:
        req = TransferRequest()
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    job_paths = JobPaths.for_id(job_id)
    # #365 step 5b: flag-aware src resolution. Helper returns
    # paths.transient under the production default; only the opt-in
    # MKVAUTO_RENAME_DIRECT_TO_DEST=1 + local-mode branch retargets to
    # config.transfer_dir. See workers.tasks._resolve_transfer_src_root.
    from workers.tasks import _resolve_transfer_src_root
    src_root = _resolve_transfer_src_root(job, job_paths, db).resolve()
    if not src_root.exists():
        raise HTTPException(400, detail=f"Transfer source path not found: {src_root}")

    # Get active transfer config - required
    config = transfer_service.get_active_config(db)
    if not config:
        try:
            from core.notifications import emit_notification_sync
            emit_notification_sync(
                "No transfer destination. Set up a transfer in Settings > Transfer Configs.",
                "warning",
                "action_required",
                job_id=job_id,
                action_type="open_transfer_setup",
                action_payload={},
            )
        except Exception as e:
            log.warning("Failed to emit action_required notification: %s", e)
        raise HTTPException(
            400, 
            detail="No active transfer configuration found. Please configure a transfer method in Settings > Transfer Configs. "
                   "You can create a local, rsync, SMB, or NFS transfer configuration and activate it."
        )
    
    # Allow override for local mode if target_dir is provided in request
    if config.mode == "local" and req.target_dir:
        config.transfer_dir = req.target_dir
    elif config.mode == "local" and target_dir:
        config.transfer_dir = target_dir

    if config.mode == "local" and (not getattr(config, "transfer_dir", None) or not str(config.transfer_dir).strip()):
        raise HTTPException(
            400,
            detail="Local transfer requires a destination path. Set Transfer Path in Settings > Transfer Configs for the active config.",
        )

    # Build job metadata for path templates
    job_metadata = _build_job_metadata(job)
    
    # Determine what to transfer
    output_files = None
    try:
        output_files = (job.disc_payload or {}).get("output_files")
    except Exception:
        output_files = None
    # Check if we have post_paths (post-processed files) or ripped_files (raw files)
    use_final_map = bool(output_files or getattr(job, "post_paths", None) or getattr(job, "ripped_files", None))
    library_dirs = [p for p in (src_root / "Movies", src_root / "Series") if p.is_dir()]

    # Phase 2 collapse (#365): the prep work that ran under start_transfer
    # already set transfer_phase="preparing"; advance it now that the
    # actual file movement is about to begin. The helper is idempotent +
    # exception-safe so an in-flight transfer never crashes on a state
    # write hiccup.
    _advance_transfer_phase(db, job, "transferring", reason="transfer: entering transferring sub-phase")

    # Enumerate files before transfer to know count for hash step
    from core.transfer.service import enumerate_transfer_files, ProgressThrottle, verify_transferred_files_batch

    transfer_files = enumerate_transfer_files(src_root, job)
    num_files = len(transfer_files)
    
    # For library directories, we need special handling (transfer each separately)
    # For now, use the transfer service for the main transfer logic
    # Set up progress and speed callbacks with step-based progress
    # Transfer step: 0-50%, Hash step: 50-100%
    
    # Base progress callback that updates database
    def base_progress_callback(pct: int):
        """Update job progress in database."""
        try:
            apply_job_state(
                db,
                job,
                updates={
                    "transfer_progress": pct,
                    "transfer_state": "running",
                },
                reason="transfer progress",
            )
        except Exception:
            # Fallback if state update fails
            job.transfer_progress = pct
            job.transfer_state = "running"
            db.commit()
    
    # Throttled progress callback for smooth updates
    progress_throttle = ProgressThrottle(base_progress_callback, min_change=1, min_interval=0.2)
    
    # Transfer progress callback: maps transfer progress (0-100%) to overall (0-50%)
    def transfer_progress_callback(pct: int):
        """Map transfer progress to 0-50% range."""
        overall = int(pct * 50 / 100)
        progress_throttle.update(overall)
    
    # Hash progress callback: receives progress in 50-100% range
    def hash_progress_callback(progress_pct: int, filename: str):
        """Hash verification progress (already in 50-100% range).

        First invocation also advances the Phase 2 sub-phase indicator
        (#365) from "transferring" to "verifying" so the frontend's
        transferPhaseLabel switches to "Verifying integrity…".
        """
        _advance_transfer_phase(db, job, "verifying", reason="transfer: entering verifying sub-phase")
        progress_throttle.update(progress_pct)

    def speed_callback(speed_mbps: float):
        """Update transfer speed."""
        try:
            job.transfer_speed_mbps = speed_mbps
            db.commit()
        except Exception:
            pass
    
    # Generate expected output structure before transfer starts
    try:
        from core.stage_validation import generate_expected_transfer_output
        expected_output = generate_expected_transfer_output(job, db)
        # Store expected output in disc_payload for validation later
        disc_payload = job.disc_payload or {}
        disc_payload["expected_transfer_output"] = expected_output
        job.disc_payload = disc_payload
        db.commit()
        db.refresh(job)
    except Exception as exc:
        log.warning(f"Failed to generate expected transfer output: {exc}", exc_info=True)
    
    # Create checkpoint BEFORE state change so we can restore the previous state (after post-processing, before transfer)
    # We back up transient directory because that's where files are after post-processing, before transfer
    try:
        backup_dir = create_stage_backup(job_id, "transfer", db, reason="before transfer stage")
        if backup_dir:
            paths = JobPaths.from_job(job)
            paths.ensure_layout()
            # Back up transient directory (files after post-processing, before transfer)
            if paths.transient.exists() and any(paths.transient.rglob("*.mkv")):
                backup_files(paths.transient, backup_dir)
                log.info("Created checkpoint of transient directory before transfer")
    except Exception as exc:
        log.warning(f"Failed to create checkpoint for transfer: {exc}")
        # Don't block transfer if backup fails
    
    # Start transfer
    try:
        try:
            from core.notifications import emit_notification_sync
            from core.pipeline_notification_labels import job_audience_label

            disc = getattr(job, "disc", None)
            info_title = getattr(disc, "info_title", None) if disc else None
            label = job_audience_label(job, disc)
            emit_notification_sync(
                f"Transfer started: {label}",
                "info",
                "transfer_started",
                job_id=job_id,
                info_title=info_title,
            )
        except Exception as e:
            log.warning("Failed to emit transfer_started notification: %s", e)
        # Refuse if a transfer is already claimed for this job. Without this
        # the post-process auto-dispatch could already have a transfer_remote
        # in flight and we would enqueue a second one, putting two smbclient
        # processes on the same destination file.
        from core.job_state import claim_transfer_for_dispatch

        if not claim_transfer_for_dispatch(db, str(job.id)):
            raise HTTPException(
                409,
                detail=(
                    "A transfer is already in progress for this job "
                    f"(transfer_state={getattr(job, 'transfer_state', None)})."
                ),
            )
        apply_job_state(
            db,
            job,
            updates={
                "transfer_state": "running",
                "transfer_progress": 0,
                "transfer_error": None,
                "transfer_retry_count": 0,
                "workflow_step": "transfer",
                "phase": "transfer",
            },
            reason="transfer started",
            skip_context_changed=True,
        )
    except StateViolation as exc:
        raise HTTPException(409, detail=str(exc)) from exc

    # #365 step 5b'b — src==dest short-circuit. Under
    # MKVAUTO_RENAME_DIRECT_TO_DEST=1 + local mode the rename step
    # wrote directly to config.transfer_dir, so transfer has nothing to
    # copy. Must run before the scenario picker below: library_dirs
    # only works by accident, use_final_map crashes with SameFileError,
    # and regular self-nests the destination. See helper docstring.
    if _try_src_equals_dest_shortcut(job, db, src_root, config, job_metadata):
        return get_status(job_id, db)

    # Determine transfer scenario
    # Scenario 1: Library directories (Movies/Series) - merge into destination root
    # Scenario 2: final_paths mapping - selective file transfer
    # Scenario 3: Regular directory transfer

    # For remote transfers (rsync/SMB/NFS), run in background
    if config.mode in ("rsync", "smb", "nfs"):
        def _run_background_transfer(job_id_str: str, src: Path, transfer_config: db_models.TransferConfig):
            """Background task for remote transfers."""
            session = database.SessionLocal()
            try:
                job_row = crud.get_job(session, job_id_str)
                if not job_row:
                    return
                
                # Build metadata for path templates
                job_meta = _build_job_metadata(job_row)
                
                # Enumerate files for hash verification step
                from core.transfer.service import enumerate_transfer_files, ProgressThrottle, verify_transferred_files_batch
                transfer_files = enumerate_transfer_files(src, job_row)
                num_files = len(transfer_files)

                # Phase 2 collapse (#365): advance sub-phase indicator for
                # the bg (rsync/smb/nfs) path — mirrors what the sync
                # path does right after preflight.
                _advance_transfer_phase(session, job_row, "transferring", reason="bg transfer: entering transferring sub-phase")

                # Base progress callback that updates database
                def bg_base_progress_cb(pct: int):
                    try:
                        apply_job_state(
                            session,
                            job_row,
                            updates={
                                "transfer_progress": pct,
                                "transfer_state": "running",
                            },
                            reason="transfer progress",
                        )
                    except Exception:
                        job_row.transfer_progress = pct
                        job_row.transfer_state = "running"
                        session.commit()
                
                # Throttled progress callback
                bg_progress_throttle = ProgressThrottle(bg_base_progress_cb, min_change=1, min_interval=0.2)
                
                # Transfer progress callback: maps transfer progress (0-100%) to overall (0-50%)
                def bg_transfer_progress_cb(pct: int):
                    """Map transfer progress to 0-50% range."""
                    overall = int(pct * 50 / 100)
                    bg_progress_throttle.update(overall)
                
                # Hash progress callback: receives progress in 50-100% range
                def bg_hash_progress_cb(progress_pct: int, filename: str):
                    """Hash verification progress (already in 50-100% range).

                    First invocation advances the Phase 2 sub-phase
                    indicator (#365) to "verifying" via the same helper
                    the sync path uses.
                    """
                    _advance_transfer_phase(session, job_row, "verifying", reason="bg transfer: entering verifying sub-phase")
                    bg_progress_throttle.update(progress_pct)
                
                # Speed callback
                def bg_speed_cb(speed_mbps: float):
                    try:
                        job_row.transfer_speed_mbps = speed_mbps
                        session.commit()
                    except Exception:
                        pass
                
                # Record transfer start in history
                history_id = None
                try:
                    from core.transfer.utils import history as transfer_history
                    # Construct destination path for history logging
                    config_data = transfer_config.config_data or {}
                    if transfer_config.mode in ("smb", "nfs"):
                        host = config_data.get("host", "")
                        share = config_data.get("share", "")
                        path = config_data.get("path", "")
                        dest_path_str = f"{transfer_config.mode}://{host}/{share}"
                        if path:
                            dest_path_str = f"{dest_path_str}/{path}".rstrip("/")
                    elif transfer_config.mode == "rsync":
                        host = config_data.get("host", "")
                        path = config_data.get("path", "")
                        dest_path_str = f"rsync://{host}/{path}".rstrip("/") if path else f"rsync://{host}"
                    else:  # local
                        dest_path_str = config_data.get("transfer_dir", "") or str(src)
                    history_id = transfer_history.log_transfer_start(
                        session,
                        job_id=job_id_str,
                        config_id=str(transfer_config.id),
                        mode=transfer_config.mode,
                        source_path=str(src),
                        dest_path=dest_path_str
                    )
                except Exception as hist_exc:
                    log.warning(f"[{job_id_str}] Failed to log transfer start: {hist_exc}")
                
                # Execute transfer
                try:
                    result = transfer_service.execute_transfer(
                        session,
                        job_id_str,
                        src,
                        transfer_config,
                        progress_callback=bg_transfer_progress_cb,
                        speed_callback=bg_speed_cb
                    )
                    # Contract: success=True must include non-empty dest_path (audit: Backend/core/transfer/protocols/)
                    if result.get("success"):
                        dp = result.get("dest_path")
                        if not dp or not str(dp).strip():
                            result = {"success": False, "error": "Transfer layer returned success without destination path"}
                    
                    # After transfer completes, verify hashes (50-100% progress)
                    if result.get("success") and num_files > 0:
                        # Get expected hashes from source files
                        expected_hashes = {}
                        for file_path in transfer_files:
                            try:
                                from core.transfer.validation import calculate_file_hash
                                file_key = file_path.name
                                expected_hashes[file_key] = calculate_file_hash(file_path)
                            except Exception as e:
                                log.warning(f"[{job_id_str}] Could not calculate source hash for {file_path}: {e}")
                        
                        # Get destination path for hash verification
                        dest_path = Path(result.get("dest_path", ""))
                        if dest_path.exists():
                            # Enumerate transferred files at destination
                            if dest_path.is_file():
                                dest_files = [dest_path]
                            else:
                                # Find MKV files in destination
                                dest_files = list(dest_path.rglob("*.mkv"))
                            
                            # Verify hashes with progress
                            if dest_files and expected_hashes:
                                verify_results = verify_transferred_files_batch(
                                    dest_files,
                                    expected_hashes,
                                    progress_cb=bg_hash_progress_cb
                                )
                                
                                # Check if all verifications passed
                                all_verified = all(v is True for v in verify_results.values() if v is not None)
                                if all_verified:
                                    result["verified"] = True
                                elif any(v is False for v in verify_results.values()):
                                    result["verified"] = False
                                    log.warning(f"[{job_id_str}] Some hash verifications failed")
                                # If some are None (no expected hash), keep result["verified"] as is
                    
                    if not result.get("success"):
                        error_msg = result.get("error") or "Transfer failed"
                        transfer_log.error("Job %s: transfer failed: %s", job_id_str, error_msg)
                        if _transfer_failure_maybe_retry(session, job_row, error_msg, None, history_id, job_id_str):
                            session.close()
                            time.sleep(5)
                            _run_background_transfer(job_id_str, src, transfer_config)
                        return
                    
                    transfer_paths = [result.get("dest_path", "")]
                    ok, verify_error = _verify_transfer_destination(transfer_paths, job_row, session)
                    if not ok:
                        err = verify_error or "Transfer destination verification failed"
                        transfer_log.warning("Job %s: %s", job_id_str, err)
                        if _transfer_failure_maybe_retry(session, job_row, err, transfer_paths, history_id, job_id_str):
                            session.close()
                            time.sleep(5)
                            _run_background_transfer(job_id_str, src, transfer_config)
                        return
                    
                    # Determine next phase
                    profile = job_row.stage_profile or "miss"
                    next_job_status = job_row.job_status
                    next_phase = "transfer"
                    next_finalize_release_state = job_row.finalize_release_state
                    if profile == "hit" or job_row.finalize_release_state in ("completed", "skipped"):
                        next_job_status = "completed"
                        next_phase = "complete"
                        next_finalize_release_state = next_finalize_release_state or "skipped"
                    elif profile == "miss":
                        next_phase = "complete"  # Skip finalize_release phase, go directly to complete
                    
                    # Update job state.
                    # #365 step 3d — no more post_state column write (same
                    # rationale as the sync transfer-complete path; see
                    # _complete_transfer).
                    completion_stage_updates: dict[str, Any] = {}
                    if next_job_status == "completed":
                        completion_stage_updates = {
                            "rip_state": getattr(job_row, "rip_state", None) or ("completed" if (job_row.rip_progress or 0) >= 100 else None),
                            "transfer_state": "completed",
                            "label_state": getattr(job_row, "label_state", None) or ("skipped" if (profile or "").lower() == "hit" else None),
                            "finalize_state": getattr(job_row, "finalize_state", None) or ("skipped" if (profile or "").lower() == "hit" else None),
                        }
                    
                    apply_job_state(
                        session,
                        job_row,
                        updates={
                            "transfer_state": "completed",
                            "transfer_progress": 100,
                            "transfer_paths": transfer_paths,
                            "transfer_error": None,
                            "transfer_verification_hash": result.get("source_hash"),
                            "transfer_verification_status": "verified" if result.get("verified") else "pending",
                            "job_status": next_job_status,
                            "phase": next_phase,
                            "finalize_release_state": next_finalize_release_state,
                            **{k: v for k, v in completion_stage_updates.items() if v is not None},
                        },
                        reason="transfer completed",
                    )
                    
                    # Record transfer completion in history
                    if history_id:
                        try:
                            from core.transfer.utils import history as transfer_history
                            transfer_history.log_transfer_complete(
                                session,
                                history_id=history_id,
                                bytes_transferred=result.get("bytes_transferred", 0),
                                duration=result.get("duration", 0),
                                verified=result.get("verified", False),
                                hash_value=result.get("source_hash")
                            )
                        except Exception as hist_exc:
                            log.warning(f"[{job_id_str}] Failed to log transfer completion: {hist_exc}")
                    
                    # Check storage after transfer completes
                    try:
                        from core.transfer.service import get_active_config, check_storage
                        active_config = get_active_config(session)
                        if active_config and active_config.mode in ("smb", "nfs", "rsync"):
                            storage_info, error = check_storage(session, active_config)
                            if error:
                                logging.warning(f"[{job_id_str}] Could not check storage after transfer: {error}")
                            elif storage_info:
                                free_gb = storage_info.get("free", 0) / (1024 ** 3)
                                logging.info(f"[{job_id_str}] Transfer destination storage after transfer: {free_gb:.2f} GB free")
                    except Exception as e:
                        logging.warning(f"[{job_id_str}] Failed to check storage after transfer: {e}")
                    transfer_log.info("Job %s: %s transfer completed", job_id_str, transfer_config.mode)
                except Exception as exc:
                    transfer_log.error("Job %s: %s transfer failed: %s", job_id_str, transfer_config.mode, exc)
                    if _transfer_failure_maybe_retry(session, job_row, str(exc), None, history_id, job_id_str):
                        session.close()
                        time.sleep(5)
                        _run_background_transfer(job_id_str, src, transfer_config)
            finally:
                session.close()
        
        # Kick off transfer via Celery task (#321/#50)
        # Falls back to inline background transfer if Celery is unavailable
        try:
            from workers.tasks import transfer_remote
            task_result = transfer_remote.delay(str(job.id), str(src_root), str(config.id))
            log.info("Job %s: enqueued transfer_remote task (task_id=%s, config=%s)",
                     job.id, task_result.id if task_result else "unknown", config.mode)
        except Exception as enqueue_exc:
            log.warning("Job %s: failed to enqueue Celery transfer task (%s); falling back to background task",
                        job.id, enqueue_exc)
            if background_tasks is not None:
                background_tasks.add_task(_run_background_transfer, str(job.id), src_root, config)
            else:
                _run_background_transfer(str(job.id), src_root, config)
        
        db.refresh(job)
        return get_status(job_id, db)
    
    # For local transfers, handle synchronously with special cases
    # Step 1a: Handle library directories (Movies/Series) - merge into destination
    if library_dirs and config.mode == "local":
        dests: list[str] = []
        try:
            # Library directories need special handling - merge into destination root
            dest_root = Path(config.transfer_dir).resolve()
            dest_root.mkdir(parents=True, exist_ok=True)
            
            transfer_log.info(
                "Job %s: transferring library directories %s -> %s",
                job.id, [str(p) for p in library_dirs], dest_root
            )
            
            # Calculate total size for progress tracking
            total_bytes = 0
            for lib_dir in library_dirs:
                for dirpath, _, filenames in os.walk(lib_dir):
                    for fn in filenames:
                        src = Path(dirpath) / fn
                        try:
                            total_bytes += src.stat().st_size
                        except FileNotFoundError:
                            continue
            
            # Validate space
            try:
                usage = shutil.disk_usage(dest_root)
                if usage.free < total_bytes:
                    raise HTTPException(400, detail=f"Not enough free space in target {dest_root} (need {total_bytes} bytes, have {usage.free})")
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(400, detail=f"Failed to check free space: {exc}")
            
            copied_bytes = 0
        except HTTPException:
            raise
        except Exception as exc:
            _fail_transfer(job, db, str(exc), dests)
            transfer_log.exception("Job %s: library transfer failed (setup): %s", job.id, exc)
            raise HTTPException(500, detail=f"Transfer failed: {exc}")
        
        try:
            for lib_dir in library_dirs:
                lib_name = lib_dir.name  # "Movies" or "Series"
                dest_lib_dir = dest_root / lib_name
                
                # Copy all files from library directory, preserving structure
                for dirpath, _, filenames in os.walk(lib_dir):
                    rel_dir = Path(dirpath).relative_to(lib_dir)
                    dest_dir = dest_lib_dir / rel_dir
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    
                    for fn in filenames:
                        src_file = Path(dirpath) / fn
                        dst_file = dest_dir / fn
                        
                        # Skip if already exists (merge behavior)
                        if dst_file.exists():
                            continue
                        
                        shutil.copy2(src_file, dst_file)
                        copied_bytes += src_file.stat().st_size
                        
                        # Update progress (map to 0-50% range for transfer step)
                        transfer_pct = int(min(100, (copied_bytes / max(1, total_bytes)) * 100))
                        overall_pct = int(transfer_pct * 50 / 100)  # Map to 0-50%
                        transfer_progress_callback(overall_pct)
                
                dests.append(str(dest_lib_dir))
            
            # After transfer completes, verify hashes (50-100% progress)
            # Enumerate transferred files
            dest_files = []
            for dest_dir in dests:
                dest_path = Path(dest_dir)
                if dest_path.exists():
                    dest_files.extend(list(dest_path.rglob("*.mkv")))
            
            # Get expected hashes from source files
            expected_hashes = {}
            for lib_dir in library_dirs:
                for file_path in lib_dir.rglob("*.mkv"):
                    try:
                        from core.transfer.validation import calculate_file_hash
                        file_key = file_path.name
                        expected_hashes[file_key] = calculate_file_hash(file_path)
                    except Exception as e:
                        log.warning(f"[{job.id}] Could not calculate source hash for {file_path}: {e}")
            
            # Verify hashes with progress
            if dest_files and expected_hashes:
                verify_results = verify_transferred_files_batch(
                    dest_files,
                    expected_hashes,
                    progress_cb=hash_progress_callback
                )
                
                # Check if all verifications passed
                all_verified = all(v is True for v in verify_results.values() if v is not None)
                if all_verified:
                    log.info(f"Job {job.id}: All hash verifications passed")
                elif any(v is False for v in verify_results.values()):
                    log.warning(f"Job {job.id}: Some hash verifications failed")
            
            # Verify destination before marking complete
            ok, verify_error = _verify_transfer_destination(dests, job, db)
            if not ok:
                _fail_transfer(job, db, verify_error or "Transfer destination verification failed", dests)
                transfer_log.warning("Job %s: %s", job.id, verify_error)
                raise HTTPException(500, detail=verify_error or "Transfer failed")
            
            # Complete transfer
            _complete_transfer(job, db, dests, job_metadata)
            transfer_log.info("Job %s: library transfer completed -> %s", job.id, dests)
            return get_status(job_id, db)
            
        except Exception as exc:
            _fail_transfer(job, db, str(exc), dests)
            transfer_log.exception("Job %s: library transfer failed: %s", job.id, exc)
            raise HTTPException(500, detail=f"Transfer failed: {exc}")
    
    # Step 1b: Handle final_paths mapping - selective file transfer.
    # #365 § 6.1 — extracted to _execute_local_transfer_use_final_map so the
    # same body is callable from the start_transfer worker for auto-progression.
    elif use_final_map and config.mode == "local":
        _execute_local_transfer_use_final_map(
            db, job, src_root, config, output_files, job_metadata,
            transfer_progress_callback=transfer_progress_callback,
            hash_progress_callback=hash_progress_callback,
        )
        return get_status(job_id, db)
    
    # Step 1c: Regular directory transfer using transfer service
    else:
        # Use the transfer service for regular transfers
        try:
            result = transfer_service.execute_transfer(
                db,
                job_id,
                src_root,
                config,
                progress_callback=transfer_progress_callback,
                speed_callback=speed_callback
            )
            
            if not result.get("success", False):
                error_msg = result.get("error", "Transfer failed")
                _fail_transfer(job, db, error_msg)
                raise HTTPException(500, detail=f"Transfer failed: {error_msg}")
            
            # After transfer completes, verify hashes (50-100% progress)
            if num_files > 0:
                # Get expected hashes from source files
                expected_hashes = {}
                for file_path in transfer_files:
                    try:
                        from core.transfer.validation import calculate_file_hash
                        file_key = file_path.name
                        expected_hashes[file_key] = calculate_file_hash(file_path)
                    except Exception as e:
                        log.warning(f"[{job.id}] Could not calculate source hash for {file_path}: {e}")
                
                # Get destination path for hash verification
                dest_path = Path(result.get("dest_path", ""))
                if dest_path.exists():
                    # Enumerate transferred files at destination
                    if dest_path.is_file():
                        dest_files = [dest_path]
                    else:
                        # Find MKV files in destination
                        dest_files = list(dest_path.rglob("*.mkv"))
                    
                    # Verify hashes with progress
                    if dest_files and expected_hashes:
                        verify_results = verify_transferred_files_batch(
                            dest_files,
                            expected_hashes,
                            progress_cb=hash_progress_callback
                        )
                        
                        # Check if all verifications passed
                        all_verified = all(v is True for v in verify_results.values() if v is not None)
                        if all_verified:
                            result["verified"] = True
                        elif any(v is False for v in verify_results.values()):
                            result["verified"] = False
                            log.warning(f"[{job.id}] Some hash verifications failed")
            
            # Update job with transfer results
            dest_paths = [result.get("dest_path", "")]
            ok, verify_error = _verify_transfer_destination(dest_paths, job, db)
            if not ok:
                _fail_transfer(job, db, verify_error or "Transfer destination verification failed", dest_paths)
                transfer_log.warning("Job %s: %s", job.id, verify_error)
                raise HTTPException(500, detail=verify_error or "Transfer failed")
            
            job.transfer_verification_hash = result.get("source_hash")
            job.transfer_verification_status = "verified" if result.get("verified") else "pending"
            job.transfer_speed_mbps = result.get("speed_mbps")
            job.transfer_bytes_transferred = result.get("bytes_transferred")
            
            _complete_transfer(job, db, dest_paths, job_metadata)
            transfer_log.info("Job %s: transfer completed -> %s", job.id, dest_paths)
            return get_status(job_id, db)
            
        except HTTPException:
            raise
        except Exception as exc:
            _fail_transfer(job, db, str(exc))
            transfer_log.exception("Job %s: transfer failed: %s", job.id, exc)
            raise HTTPException(500, detail=f"Transfer failed: {exc}")


@router.post("/{job_id}/transfer/retry", response_model=JobStatus)
def retry_transfer(job_id: str, db: Session = Depends(get_db)) -> JobStatus:
    """Retry a failed transfer."""
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    try:
        transfer_error_handler.retry_transfer(db, job_id)
    except ValueError as e:
        raise HTTPException(400, detail=str(e))
    
    return get_status(job_id, db)


@router.post("/{job_id}/transfer/verify", response_model=JobStatus)
def verify_transfer(job_id: str, db: Session = Depends(get_db)) -> JobStatus:
    """Manually trigger hash verification for a transferred file."""
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    if not job.transfer_verification_hash:
        raise HTTPException(400, detail="No verification hash stored for this transfer")
    
    # Get active config
    config = transfer_service.get_active_config(db)
    if not config:
        raise HTTPException(400, detail="No active transfer config")
    
    # Verify transfer (implementation depends on transfer mode)
    # For now, return current status
    return get_status(job_id, db)


@router.post("/{job_id}/transfer/cleanup", response_model=JobStatus)
def cleanup_source(job_id: str, db: Session = Depends(get_db)) -> JobStatus:
    """Manually trigger source cleanup after successful transfer. Enqueues Celery task; returns immediately."""
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    if job.transfer_state != "completed":
        raise HTTPException(400, detail="Transfer must be completed before cleanup")
    
    config = transfer_service.get_active_config(db)
    if not config:
        raise HTTPException(400, detail="No active transfer config")
    
    if JobPaths.for_id(job_id).root.exists():
        cleanup_job_mkv.delay(job_id, "transfer_cleanup")
    return get_status(job_id, db)


@router.post("/{job_id}/transfer/validate", response_model=ValidationResult)
def validate_transfer_preconditions(job_id: str, db: Session = Depends(get_db)) -> ValidationResult:
    """Run pre-transfer validation checks."""
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    config = transfer_service.get_active_config(db)
    if not config:
        return ValidationResult(success=False, message="No active transfer config", errors=["No active transfer config"])
    
    # Calculate source size from transient directory.
    # #365 step 5b: this preflight intentionally walks paths.transient
    # (not _resolve_transfer_src_root) because under the opt-in
    # MKVAUTO_RENAME_DIRECT_TO_DEST=1 flag the helper would resolve to
    # the shared library root — walking it would size the entire
    # library, not this rip. Under flag-on transient/ is empty, so the
    # disk-space precondition is vacuously satisfied — which matches
    # reality since the flag-on transfer is a no-op (files were renamed
    # directly to the destination already). A 5b' follow-up will add
    # per-rip-aware sizing here once the src==dest shortcut lands in
    # transfer_job.
    from core.job_paths import JobPaths
    job_paths = JobPaths.from_job(job)
    src_path = job_paths.transient
    source_size = 0
    try:
        if src_path.is_file():
            source_size = src_path.stat().st_size
        else:
            for dirpath, _, filenames in os.walk(src_path):
                for fn in filenames:
                    try:
                        source_size += (Path(dirpath) / fn).stat().st_size
                    except FileNotFoundError:
                        continue
    except Exception as e:
        return ValidationResult(success=False, message=f"Could not calculate source size: {e}", errors=[str(e)])
    
    passed, errors = transfer_service.validate_transfer_preconditions(db, job_id, config, source_size)
    
    # Update job validation status
    job.transfer_validation_status = "passed" if passed else "failed"
    job.transfer_validation_error = "; ".join(errors) if errors else None
    db.commit()
    
    return ValidationResult(success=passed, message="Validation passed" if passed else "Validation failed", errors=errors if not passed else None)


@router.get("/{job_id}/validate-postprocess-preconditions", response_model=ValidationResult)
def validate_postprocess_preconditions_endpoint(job_id: str, db: Session = Depends(get_db)) -> ValidationResult:
    """
    Run pre-flight validation checks for postprocess stage.
    
    This is a read-only diagnostic endpoint that checks if postprocess can start successfully.
    It does NOT change job state.
    
    Returns:
        ValidationResult with success=True if postprocess can start, False otherwise
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    try:
        from core.stage_validation import validate_transfer_preconditions
        paths = JobPaths.from_job(job)
        validation_result = validate_transfer_preconditions(job, db, paths)
        
        # Convert ValidationResult (from stage_validation) to ValidationResult (from schemas)
        # stage_validation.ValidationResult uses 'valid', schemas.ValidationResult uses 'success'
        message = "Validation passed" if validation_result.valid else "Validation failed"
        if validation_result.warnings:
            message += f" (warnings: {len(validation_result.warnings)})"
        
        return ValidationResult(
            success=validation_result.valid,
            message=message,
            errors=validation_result.errors if not validation_result.valid else None
        )
    except Exception as exc:
        log.error(f"Job {job_id}: Pre-flight validation endpoint error: {exc}", exc_info=True)
        return ValidationResult(
            success=False,
            message=f"Validation error: {exc}",
            errors=[str(exc)]
        )


@router.post("/{job_id}/resume", response_model=JobStatus)
def resume_job(job_id: str, db: Session = Depends(get_db)):
    """
    Resume a job that finished ripping but failed or got stuck during post-processing/move.
    This only triggers the post-process resume task; it will not restart an
    in-progress rip.
    
    Can recover from:
    - Failed post-processing
    - Stuck post-processing (post_state="running" but no progress)
    - Stuck finalize->post-process transition
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    # Allow failed, pending, running, or validating (validating is interchangeable with running)
    if job.job_status not in ("failed", "pending", "running", "validating"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; resume only allowed for failed/pending/running/validating post-process")
    
    # Check if rip is completed
    if getattr(job, "rip_state", None) not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed before resuming post-process")
    
    # Allow resume from "ready", "pending", "running" (stuck), or "failed" states.
    # #365 — derived, not column.
    post_state = job.derived_post_state
    if post_state == "running":
        log.warning("Job %s: Resuming stuck post-process (post_state=running)", job.id)
        # Use devmode to allow backward transition (running -> pending -> running)
    elif post_state not in ("pending", "ready", "failed"):
        raise HTTPException(400, detail=f"Post-process is {post_state}; resume only allowed when pending/ready/failed/running")
    else:
        # Normal resume case - use devmode if job is failed to allow backward transition
        if job.job_status == "failed":
            pass
        else:
            pass  # Job not failed; postprocess_started below sets running

    try:
        StageState.postprocess_started(db, job, reason="resume postprocess requested", error_reason=None)
    except StateViolation as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    # Phase 2 collapse (#365): the unified start_transfer worker is the
    # canonical entry point. resume_postprocess still works as a forwarding
    # shim, but new sites target start_transfer directly.
    from workers.tasks import start_transfer as start_transfer_task
    start_transfer_task.delay(str(job.id))
    log.info("Job %s: Enqueued start_transfer task for recovery", job.id)
    return get_status(job_id, db)


@router.post("/{job_id}/postprocess", response_model=JobStatus)
def start_postprocess(job_id: str, db: Session = Depends(get_db)):
    """
    Manually trigger post-process stage for a job.
    
    Prerequisites:
    - job_status must not be failed
    - rip_state must be completed or skipped
    - post_state must be ready or pending (not already running/completed)
    
    This will:
    - Set post_state to running
    - Set phase to postprocess
    - Enqueue the resume_postprocess Celery task
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    # Validate job is not failed
    job_status = getattr(job, "job_status", None)
    if job_status == "failed":
        raise HTTPException(400, detail=f"Job is failed; cannot start post-process")
    
    # Validate prerequisites
    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail=f"Rip must be completed before starting post-process (current rip_state: {rip_state})")

    # #365 step 4 — derived, not column.
    post_state = job.derived_post_state
    if post_state in ("running", "completed"):
        raise HTTPException(400, detail=f"Post-process is already {post_state}; cannot start")

    # Validate post_state is ready or pending
    if post_state not in ("ready", "pending", None):
        raise HTTPException(400, detail=f"Post-process state must be ready or pending to start (current: {post_state})")
    
    # Note: Pre-flight validation is now done inside resume_postprocess task AFTER devmode prep
    # This ensures validation uses the correct mkv_size values (mock sizes in devmode, real sizes otherwise)
    
    try:
        # #365 Phase 2 § 6.4 — workflow_step="transfer" instead of
        # "postprocess" since the standalone step was collapsed.
        StageState.postprocess_started(
            db,
            job,
            reason="manual post-process start requested",
            workflow_step="transfer",
            error_reason=None,
        )
        log.info(f"Job {job_id}: start_postprocess: postprocess_started, Starting post-process manually")
    except StateViolation as exc:
        log.warning("Job %s: start_postprocess: postprocess_started StateViolation: %s", job_id, exc)
        raise HTTPException(409, detail=str(exc)) from exc

    # Enqueue the post-process task via the unified start_transfer worker
    # (Phase 2 collapse, #365). start_transfer sets transfer_phase=preparing
    # and currently delegates to resume_postprocess so the prep body is
    # unchanged. The endpoint name stays the same for backward compat.
    try:
        from workers.tasks import start_transfer as start_transfer_task
        task_result = start_transfer_task.delay(str(job.id))
        log.info(f"Job {job_id}: Enqueued start_transfer task (task_id={task_result.id if task_result else 'unknown'})")
    except Exception as enqueue_exc:
        log.error(f"Job {job_id}: Failed to enqueue resume_postprocess: {enqueue_exc}", exc_info=True)
        # Rollback state change if enqueue fails
        try:
            apply_job_state(
                db,
                job,
                updates={
                    "error_reason": f"Failed to enqueue post-process task: {enqueue_exc}",
                },
                reason="rollback after enqueue failure",
                skip_context_changed=True,
            )
        except Exception:
            pass
        raise HTTPException(500, detail=f"Failed to enqueue post-process task: {enqueue_exc}") from enqueue_exc
    
    return get_status(job_id, db)


@router.post("/{job_id}/reset-postprocess", response_model=JobStatus)
def reset_postprocess(job_id: str, clear_files: bool = False, backup_files_param: bool = True, db: Session = Depends(get_db)):
    """
    DEV MODE ONLY: Reset post-processing state to allow re-running post-processing.
    This is useful for testing post-processing modifications without creating new jobs.
    
    - Resets post_state to 'pending'
    - Resets transfer_state to 'pending' (if post was completed)
    - Optionally clears transient directory files (with optional backup)
    - Sets phase back to 'postprocess' if job was in transfer/finalize_release phase
    
    Args:
        clear_files: If True, clears the transient directory after backing up (if backup_files_param=True)
        backup_files_param: If True and clear_files=True, creates a backup of transient files before clearing
    """
    if not is_dev_mode():
        raise HTTPException(403, detail="This endpoint is only available in dev mode (set ENABLE_DEVMODE=1)")


def _clear_per_rip_postprocess_output(
    job: db_models.Job, paths: Any, db: Session,
) -> int:
    """Delete this rip's post-processed files at the flag-aware rename
    destination. Returns the number of files deleted.

    Under flag-off this resolves to ``paths.transient`` and the files
    that the bulk ``shutil.rmtree`` above already removed are gone too —
    so this walk is a no-op (every ``post_paths`` entry resolves to a
    path inside the just-cleared transient). Under flag-on local the
    bulk transient clear is the no-op and this walk is what actually
    removes the per-rip slots from the shared library root.

    Safety: only paths from ``job.post_paths`` are deleted — never a
    bulk walk of the destination. Walking the shared library would
    delete unrelated rips' files; the per-rip iteration is the same
    safety pattern the postprocess validator and the transfer-step
    shortcut use.
    """
    from core.transfer.path_resolution import resolve_rename_dest_root
    post_paths = getattr(job, "post_paths", None) or {}
    if not post_paths:
        return 0
    try:
        dest_root = Path(resolve_rename_dest_root(job, paths, db)).resolve()
    except Exception as exc:
        log.warning("_clear_per_rip_postprocess_output: helper failed for job %s: %s", getattr(job, "id", "?"), exc)
        return 0
    removed = 0
    for rel in post_paths.values():
        try:
            p = (dest_root / rel).resolve()
        except Exception:
            continue
        if not p.exists() or not p.is_file():
            continue
        try:
            p.unlink()
            removed += 1
        except Exception as exc:
            log.warning("_clear_per_rip_postprocess_output: unlink %s failed: %s", p, exc)
    return removed


@router.post("/{job_id}/restore-postprocess", response_model=JobStatus)
def restore_postprocess(job_id: str, db: Session = Depends(get_db)):
    """
    DEV MODE ONLY: Restore post-processed files and database state from backup.
    This restores both the transient directory and database state from backup.
    Useful for reverting to a previous post-processing state after testing changes.
    """
    if not is_dev_mode():
        raise HTTPException(403, detail="This endpoint is only available in dev mode (set ENABLE_DEVMODE=1)")


@router.post("/{job_id}/revert-transfer", response_model=JobStatus)
def revert_transfer(job_id: str, db: Session = Depends(get_db)):
    """
    DEV MODE ONLY: Revert transfer by restoring database state from backup.
    Note: File restoration is not performed for transfers as the destination may be remote
    or the files may have been moved/deleted. Only database state is restored.
    """
    if not is_dev_mode():
        raise HTTPException(403, detail="This endpoint is only available in dev mode (set ENABLE_DEVMODE=1)")


@router.post("/{job_id}/label", response_model=JobStatus)
def save_label(job_id: str, label: LabelRequest, db: Session = Depends(get_db)):
    """
    Persist manual label data for a job (movie/series, disc/release metadata, track labels).
    Only valid for DiscDB miss profiles.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    disc = getattr(job, "disc", None)
    if not disc:
        raise HTTPException(400, detail="Job has no disc attached")

    # Check profile - labels are only for miss profiles
    from core.job_state import _infer_profile
    profile = _infer_profile(job)
    if profile != "miss":
        raise HTTPException(400, detail=f"Cannot save labels: job profile is {profile!r} (labels only for miss profile)")

    # Check that rip is completed before allowing label save
    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail=f"Cannot save labels: rip is not completed (rip_state is {rip_state!r})")

    lp = label.model_dump()
    _apply_label_to_records(disc, lp, db)
    _sync_job_disc_payload_disc_label_fields(job, disc)

    # Persist duplicate-group invariants (active/ignore + NULL primary consensus fill)
    # before validation so postprocess sees the same effective type the validator does.
    try:
        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc
        sync_duplicate_group_labels_for_disc(db, str(disc.id))
        db.flush()
    except Exception as exc:
        log.warning("save_label sync_duplicate_group_labels_for_disc failed disc_id=%s: %s", disc.id, exc)

    # Validate that all titles are properly labeled before marking as completed
    is_valid, unlabeled_title_ids = _validate_all_titles_labeled(disc, db)
    if not is_valid:
        raise HTTPException(
            400,
            detail=f"Cannot complete labeling: {len(unlabeled_title_ids)} title(s) are not properly labeled. Unlabeled title IDs: {', '.join(unlabeled_title_ids[:10])}" + (f" (and {len(unlabeled_title_ids) - 10} more)" if len(unlabeled_title_ids) > 10 else "")
        )
    
    payload = job.disc_payload or {}
    payload["label_ready"] = True
    payload["label_required"] = payload.get("label_required", True)
    job.disc_payload = payload
    finalize_state = "ready" if (job.stage_profile or "miss") == "miss" else "skipped"
    phase = "postprocess"  # Always go to postprocess after labels complete, skip finalize step
    try:
        apply_job_state(
            db,
            job,
            updates={
                "label_state": "completed",
                "finalize_state": finalize_state,
                "job_status": job.job_status or "running",
                "phase": phase,
            },
            reason="labels saved",
            skip_context_changed=True,
        )
    except StateViolation as exc:
        raise HTTPException(409, detail=str(exc)) from exc
    db.refresh(job)
    db.refresh(disc)
    return get_status(job_id, db)


def _merge_dict(target: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """
    Shallow merge update into target; nested dicts are merged recursively.
    """
    for k, v in update.items():
        if isinstance(v, dict) and isinstance(target.get(k), dict):
            target[k] = _merge_dict(target[k], v)  # type: ignore[arg-type]
        else:
            target[k] = v
    return target


@router.patch("/{job_id}/label", response_model=JobStatus)
def update_label(job_id: str, label: LabelUpdate, db: Session = Depends(get_db)):
    """
    Partial label update to support auto-save on field blur.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    # Legacy endpoint: no longer persists label_payload; rely on PATCH release/disc/track routes.
    return get_status(job_id, db)


@router.post("/{job_id}/label/complete", response_model=JobStatus)
def complete_label(
    job_id: str,
    body: Optional[Dict[str, Any]] = Body(default=None),
    db: Session = Depends(get_db)
):
    """
    Complete the label stage and transition to postprocess. Applies labels from body.labelForm
    or from label_draft, validates all titles labeled, then runs apply_job_state.
    Frontend reacts to the POST response only; workflow context updates on next page refresh.
    """
    log.info("complete_label invoked job_id=%s", job_id)
    from core.job_state import _infer_profile

    job_id_clean = str(job_id).strip() if job_id else ""
    job = db.query(db_models.Job).filter(db_models.Job.id == job_id_clean).first() if job_id_clean else None
    if not job:
        log.warning(
            "complete_label job not found job_id=%s body_keys=%s",
            job_id_clean or job_id,
            list(body.keys()) if body else [],
        )
        raise HTTPException(404, detail={"message": "Job not found", "job_id": job_id_clean or job_id})
    disc = getattr(job, "disc", None)
    if not disc:
        raise HTTPException(400, detail="Job has no disc attached")

    profile = _infer_profile(job)
    if profile != "miss":
        raise HTTPException(400, detail=f"Cannot complete label: job profile is {profile!r} (only for miss profile)")

    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail=f"Cannot complete label: rip is not completed (rip_state is {rip_state!r})")

    if body and isinstance(body.get("labelForm"), dict):
        lp = body["labelForm"].copy()
        _apply_label_to_records(disc, lp, db)
        _sync_job_disc_payload_disc_label_fields(job, disc)
        disc_payload = job.disc_payload or {}
        label_draft = disc_payload.get("label_draft") or {}
        for k in ("movie_id", "tmdb_id", "boxset_id", "release_id", "release_slug", "release_name", "release_year"):
            if k in lp:
                label_draft[k] = lp[k]
        disc_payload["label_draft"] = label_draft
        job.disc_payload = disc_payload
    else:
        disc_payload = job.disc_payload or {}
        label_draft = (disc_payload.get("label_draft") or {}).copy()
        lp = label_draft
        _apply_label_to_records(disc, lp, db)
        _sync_job_disc_payload_disc_label_fields(job, disc)

    # Re-run duplicate-group sync so post-label invariants are written to disc_titles before
    # validation and postprocess read them: in particular, NULL-typed primaries whose siblings
    # are all 'ignore' get type='ignore' persisted (apply_primary_duplicate_row's consensus
    # fill). Without this, postprocess _rename_movie sees NULL type and falls back to the
    # main-movie filename, causing destination collisions across multiple primaries.
    try:
        from core.duplicate_group_sync import sync_duplicate_group_labels_for_disc
        sync_duplicate_group_labels_for_disc(db, str(disc.id))
        db.flush()
    except Exception as exc:
        log.warning("complete_label sync_duplicate_group_labels_for_disc failed disc_id=%s: %s", disc.id, exc)

    is_valid, unlabeled_title_ids = _validate_all_titles_labeled(disc, db)
    if not is_valid:
        raise HTTPException(
            400,
            detail=f"Cannot complete labeling: {len(unlabeled_title_ids)} title(s) are not properly labeled. Unlabeled title IDs: {', '.join(unlabeled_title_ids[:10])}" + (f" (and {len(unlabeled_title_ids) - 10} more)" if len(unlabeled_title_ids) > 10 else "")
        )

    payload = job.disc_payload or {}
    payload["label_ready"] = True
    payload["label_required"] = payload.get("label_required", True)
    job.disc_payload = payload
    finalize_state = "ready" if (job.stage_profile or "miss") == "miss" else "skipped"
    # #365 Phase 2 § 6.4 — workflow_step="transfer" (postprocess collapsed).
    job.workflow_step = "transfer"
    try:
        StageState.label_complete(
            db,
            job,
            reason="labels completed via POST /label/complete",
            post_state="ready",
            finalize_state=finalize_state,
            job_status=job.job_status or "running",
        )
    except StateViolation as exc:
        log.warning("complete_label StateViolation job_id=%s detail=%s", job_id, str(exc))
        raise HTTPException(409, detail=str(exc)) from exc
    log.info("complete_label success job_id=%s", job_id)
    db.refresh(job)
    db.refresh(disc)
    return get_status(str(job.id), db)


@router.post("/{job_id}/label/finalize", response_model=JobStatus)
def finalize_label(job_id: str, db: Session = Depends(get_db)):
    """
    Finalize labeling: delegates to disc finalize using persisted records.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    disc = getattr(job, "disc", None)
    if not disc:
        raise HTTPException(400, detail="Job has no disc attached")
    return finalize_disc(str(disc.id), db)  # type: ignore[arg-type]


def _rel_safe_path(base: Path, rel_path: str) -> Path:
    """
    Resolve rel_path under base and ensure no traversal escapes base.
    """
    candidate = (base / rel_path).resolve()
    if base not in candidate.parents and candidate != base:
        raise HTTPException(400, detail="Invalid path")
    if not candidate.exists():
        raise HTTPException(404, detail="File not found")
    return candidate


@router.get("/{job_id}/tracks")
def list_tracks(job_id: str, db: Session = Depends(get_db)):
    """
    List .mkv files for a job (from tmp_dir or result_location) to support preview.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    base = _job_base_dir(job)
    if not base:
        raise HTTPException(404, detail="No files available for this job")

    tracks = []
    for dirpath, _, filenames in os.walk(base):
        for fn in filenames:
            if not fn.lower().endswith(".mkv"):
                continue
            full = Path(dirpath) / fn
            rel = full.relative_to(base)
            stat = full.stat()
            tracks.append(
                {
                    "name": fn,
                    "rel_path": str(rel),
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                }
            )
    return {"base": str(base), "tracks": tracks}


@router.get("/{job_id}/previews/status")
def preview_status(job_id: str, db: Session = Depends(get_db)):
    """
    Return preview status for a job (queued/running/completed/failed per track).
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    disc_payload = getattr(job, "disc_payload", None) or {}
    previews = disc_payload.get("previews")
    if not previews:
        return {}
    try:
        return PreviewInfo(**previews)
    except Exception:
        return previews


@router.get("/previews/queue")
def preview_queue(db: Session = Depends(get_db)):
    """
    List jobs with previews queued/running for UI queue indicator.
    """
    items = []
    jobs = db.query(crud.models.Job).all()  # type: ignore[attr-defined]
    for job in jobs:
        payload = getattr(job, "disc_payload", None) or {}
        previews = payload.get("previews") or {}
        status = previews.get("status")
        # Derive status if missing by looking at tracks
        if not status and isinstance(previews.get("tracks"), dict):
            track_states = [v.get("status") for v in previews["tracks"].values() if isinstance(v, dict)]
            if any(s in ("queued", "running") for s in track_states):
                status = "running"
            elif any(s == "failed" for s in track_states):
                status = "failed"
            elif track_states:
                status = "completed"
        if status not in ("queued", "running"):
            continue
        created = getattr(job, "created_at", None)
        if hasattr(created, "isoformat"):
            created = created.isoformat()
        updated = previews.get("updated_at")
        if hasattr(updated, "isoformat"):
            updated = updated.isoformat()
        items.append(
            {
                "jobId": str(job.id),
                "status": status,
                "disc_num": job.disc_num,
                "created_at": created,
                "updated_at": updated,
            }
        )
    def _sort_key(x):
        val = x.get("created_at")
        if isinstance(val, str):
            try:
                return datetime.datetime.fromisoformat(val)
            except Exception:
                return datetime.datetime.min
        return val or datetime.datetime.min
    items.sort(key=_sort_key)
    return {"items": items}


def stream_preview(job_id: str, rel_path: str, db: Session = Depends(get_db)):
    """
    Serve a preview HLS asset (manifest or segment) under the job base dir.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    # Use JobPaths to get the job root directory (previews are in {job_root}/previews/)
    from core.job_paths import JobPaths
    job_paths = JobPaths.from_job(job)
    base = job_paths.root
    
    if not base or not base.exists():
        raise HTTPException(404, detail="No files available for this job")
    
    # Prepend "previews/" to rel_path since the route strips it from the URL
    # URL: /jobs/{id}/previews/00100/preview.m3u8 -> rel_path: 00100/preview.m3u8
    # Need: {base}/previews/00100/preview.m3u8
    if not rel_path.startswith("previews/"):
        rel_path = f"previews/{rel_path}"
    
    target = _rel_safe_path(base, rel_path)
    suffix = target.suffix.lower()
    media_type = "application/octet-stream"
    if suffix in (".m3u8", ".m3u"):
        media_type = "application/vnd.apple.mpegurl"
    elif suffix in (".ts", ".m2ts"):
        media_type = "video/mp2t"
    elif suffix in (".jpg", ".jpeg"):
        media_type = "image/jpeg"
    elif suffix == ".png":
        media_type = "image/png"
    return FileResponse(target, media_type=media_type, filename=target.name)

# Allow path-style preview access so HLS segment requests resolve correctly.
@router.get("/{job_id}/preview/{rel_path:path}")
def stream_preview_path(job_id: str, rel_path: str, db: Session = Depends(get_db)):
    """
    Serve preview assets via path segments (e.g., /jobs/{id}/preview/previews/track/segment_000.ts).
    """
    return stream_preview(job_id, rel_path, db)

@router.get("/{job_id}/previews/{rel_path:path}")
def stream_previews_path(job_id: str, rel_path: str, db: Session = Depends(get_db)):
    """
    Serve preview assets via /previews/ path (e.g., /jobs/{id}/previews/track/preview.m3u8).
    """
    return stream_preview(job_id, rel_path, db)


@router.post("/{job_id}/recover", response_model=JobStatus)
def recover_job(job_id: str, db: Session = Depends(get_db)):
    """
    Comprehensive recovery endpoint that attempts to recover a stuck job.
    Automatically detects and recovers:
    - Stuck post-processing (post_state="running" but no progress)
    - Stuck preview generation (previews stuck in "queued"/"running")
    - Stuck finalize->post-process transition
    
    This is a convenience endpoint that combines resume and preview regeneration.
    Also validates end state after recovery.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    recovered = []
    validation_results = {}
    
    # Check if post-processing is stuck.
    # #365 — derived, not column.
    post_state = job.derived_post_state
    if post_state == "running" and getattr(job, "rip_state", None) in ("completed", "skipped"):
        try:
            resume_job(job_id, db)
            recovered.append("post-processing")
            # Validate post-processing after recovery
            try:
                from core.stage_validation import validate_transfer_prep_output
                from core.job_paths import JobPaths
                paths = JobPaths.from_job(job)
                validation_result = validate_transfer_prep_output(job, db, paths)
                validation_results["post-processing"] = {
                    "valid": validation_result.valid,
                    "errors": validation_result.errors,
                    "warnings": validation_result.warnings,
                }
                if not validation_result.valid:
                    log.warning("Job %s: Post-processing validation failed after recovery: %s", job_id, validation_result.errors)
            except Exception as exc:
                log.warning("Job %s: Failed to validate post-processing: %s", job_id, exc)
        except Exception as exc:
            log.warning("Job %s: Failed to recover post-processing: %s", job_id, exc)
    
    # Check if previews are stuck
    disc_payload = job.disc_payload or {}
    previews = disc_payload.get("previews", {})
    if isinstance(previews, dict):
        preview_status = previews.get("status")
        if preview_status in ("queued", "running") and getattr(job, "rip_state", None) in ("completed", "skipped"):
            try:
                regenerate_previews(job_id, db)
                recovered.append("previews")
                # Validate previews after recovery
                try:
                    from core.job_validation import validate_previews
                    is_valid, errors = validate_previews(job, db)
                    validation_results["previews"] = {"valid": is_valid, "errors": errors}
                    if not is_valid:
                        log.warning("Job %s: Preview validation failed after recovery: %s", job_id, errors)
                except Exception as exc:
                    log.warning("Job %s: Failed to validate previews: %s", job_id, exc)
            except Exception as exc:
                log.warning("Job %s: Failed to recover previews: %s", job_id, exc)
    
    if not recovered:
        raise HTTPException(400, detail="No recoverable issues detected. Job may not be stuck or may not be in a recoverable state.")
    
    log.info("Job %s: Recovery completed for: %s (validation: %s)", job_id, ", ".join(recovered), validation_results)
    return get_status(job_id, db)


@router.post("/{job_id}/validate", response_model=Dict[str, Any])
def validate_job(job_id: str, db: Session = Depends(get_db)):
    """
    Validate job end state:
    - Post-processing: Check directory structure, output files exist, hash verification
    - Previews: Check preview files exist for each output_file/title entry
    
    Returns validation results for troubleshooting.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    results = {}
    
    # Validate post-processing if rip is completed
    if getattr(job, "rip_state", None) in ("completed", "skipped"):
        try:
            from core.stage_validation import validate_transfer_prep_output
            from core.job_paths import JobPaths
            paths = JobPaths.from_job(job)
            validation_result = validate_transfer_prep_output(job, db, paths)
            results["post_processing"] = {
                "valid": validation_result.valid,
                "errors": validation_result.errors,
                "warnings": validation_result.warnings,
            }
        except Exception as exc:
            results["post_processing"] = {
                "valid": False,
                "errors": [f"Validation failed: {exc}"],
            }
    
    # Validate previews
    try:
        from core.job_validation import validate_previews
        is_valid, errors = validate_previews(job, db)
        results["previews"] = {
            "valid": is_valid,
            "errors": errors,
        }
    except Exception as exc:
        results["previews"] = {
            "valid": False,
            "errors": [f"Validation failed: {exc}"],
        }
    
    return results


@router.post("/{job_id}/previews/regenerate")
def regenerate_previews(job_id: str, db: Session = Depends(get_db)):
    """
    Force regeneration of previews for a job (user-initiated).
    Scans manifests on disk: marks existing previews completed and enqueues encoding only for missing ones.
    Resets preview auto-recovery counters so automatic health-check caps do not block this action.
    For a single title, prefer POST /{job_id}/previews/regenerate/{track_key} (uses per-track folder delete).
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    if job.job_status not in ("completed", "running"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; preview regeneration only allowed for completed or running jobs")

    if getattr(job, "rip_state", None) not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed before generating previews")

    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    if not post_paths and not ripped_files:
        disc_payload = job.disc_payload or {}
        post_paths = disc_payload.get("post_paths") or {}
        ripped_files = disc_payload.get("ripped_files") or {}
    if not (post_paths or ripped_files):
        raise HTTPException(400, detail="No post_paths or ripped_files found for preview generation")

    tracks_state, tracks_to_regenerate, overall_status = build_preview_regeneration_state(job, db)
    if not tracks_state:
        raise HTTPException(400, detail="No preview tracks found to check")

    disc_payload = job.disc_payload or {}
    previews = {
        "status": overall_status,
        "tracks": tracks_state,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }
    user_reset_preview_auto_recovery_metadata(previews)
    disc_payload["previews"] = previews
    job.disc_payload = disc_payload
    db.commit()
    
    if tracks_to_regenerate:
        log.info("Job %s: Regenerating %d preview(s) (recovery from stuck state)", job.id, len(tracks_to_regenerate))
        generate_previews.delay(job_id, tracks_to_regenerate)
        return {"status": "queued", "recovered": len(tracks_state) - len(tracks_to_regenerate), "regenerating": len(tracks_to_regenerate)}
    else:
        log.info("Job %s: All previews already exist, status updated to completed", job.id)
        return {"status": "completed", "recovered": len(tracks_state), "regenerating": 0}


@router.post("/{job_id}/previews/regenerate-failed")
def regenerate_failed_previews(job_id: str, db: Session = Depends(get_db)):
    """
    Batch regenerate all failed previews for a job.
    Finds all tracks with status 'failed', sets them to 'queued', and dispatches
    a generate_preview_track task for each.
    """
    from workers.tasks import _safe_track_folder, _build_title_id_maps, _resolve_preview_rel_path, _resolve_preview_title_id

    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed before generating previews")
    if job.job_status in ("failed", "pending"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; preview regeneration not allowed")

    # Get file paths for source resolution
    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    if not post_paths and not ripped_files:
        dp = job.disc_payload or {}
        post_paths = dp.get("post_paths") or {}
        ripped_files = dp.get("ripped_files") or {}
    file_paths = post_paths if post_paths else ripped_files
    if not file_paths:
        raise HTTPException(400, detail="No post_paths or ripped_files found for preview generation")

    disc_payload = job.disc_payload or {}
    preview_maps = _build_title_id_maps(job, disc_payload)
    existing_previews = disc_payload.get("previews") or {}
    existing_tracks = existing_previews.get("tracks") if isinstance(existing_previews, dict) else {}
    if not isinstance(existing_tracks, dict):
        existing_tracks = {}

    # Find all failed tracks
    queued_keys: list[str] = []
    for track_key, track_info in existing_tracks.items():
        if not isinstance(track_info, dict):
            continue
        if track_info.get("status") != "failed":
            continue

        rel_path = track_info.get("source")
        title_id = _resolve_preview_title_id(track_key, rel_path, preview_maps)
        canonical_key = title_id or track_key

        # Resolve source path
        if not rel_path:
            rel_path = _resolve_preview_rel_path(canonical_key, file_paths, preview_maps)
        if not rel_path:
            rel_path = file_paths.get(track_key)
        if not rel_path:
            continue  # Skip tracks we can't resolve

        # Update to queued
        manifest_rel = f"previews/{_safe_track_folder(canonical_key)}/preview.m3u8"
        existing_tracks[track_key] = {
            "status": "queued",
            "manifest": manifest_rel,
            "error": None,
            "title_id": title_id,
            "source": rel_path,
        }
        queued_keys.append(canonical_key)

        # Queue the task
        generate_preview_track.delay(job_id, canonical_key, rel_path, title_id=title_id)

    if not queued_keys:
        raise HTTPException(400, detail="No failed previews found to regenerate")

    previews = {
        "status": "running",
        "tracks": existing_tracks,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }
    user_reset_preview_auto_recovery_metadata(previews)
    disc_payload["previews"] = previews
    job.disc_payload = disc_payload
    db.commit()

    log.info("Job %s: Queued batch preview regeneration for %d tracks", job_id, len(queued_keys))
    return {"regenerated": len(queued_keys), "tracks": queued_keys}


@router.post("/{job_id}/previews/regenerate/{track_key}")
def regenerate_preview_track(job_id: str, track_key: str, db: Session = Depends(get_db)):
    """
    Force regeneration of a single track preview (user-initiated).
    Removes only that track's preview output folder on the worker, then re-encodes.
    Resets preview auto-recovery counters.
    """
    from workers.tasks import _safe_track_folder, _build_title_id_maps, _resolve_preview_rel_path, _resolve_preview_title_id
    
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    # Check if rip is completed first - this is the key requirement
    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed before generating previews")
    
    # Allow regeneration for jobs that have finished ripping, regardless of overall job status
    # (jobs can be "validating", "running", or "completed" after rip completes)
    if job.job_status in ("failed", "pending"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; preview regeneration not allowed for failed or pending jobs")
    
    # Get post_paths (preferred) or ripped_files (fallback) from job
    # Both use title_id keys now
    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    if not post_paths and not ripped_files:
        disc_payload = job.disc_payload or {}
        post_paths = disc_payload.get("post_paths") or {}
        ripped_files = disc_payload.get("ripped_files") or {}
    
    file_paths = post_paths if post_paths else ripped_files
    if not file_paths:
        raise HTTPException(400, detail="No post_paths or ripped_files found for preview generation")
    
    # Get existing preview status for this track
    disc_payload = job.disc_payload or {}
    preview_maps = _build_title_id_maps(job, disc_payload)
    existing_previews = disc_payload.get("previews") or {}
    existing_tracks = existing_previews.get("tracks") if isinstance(existing_previews, dict) else {}
    if not isinstance(existing_tracks, dict):
        existing_tracks = {}
    
    # Get track info first (need rel_path before resolving canonical key)
    track_info = existing_tracks.get(track_key, {})
    if not isinstance(track_info, dict):
        track_info = {}
    rel_path = track_info.get("source")
    
    # Resolve to canonical title_id key when possible
    title_id = _resolve_preview_title_id(track_key, rel_path, preview_maps)
    canonical_key = title_id or track_key

    # If canonical_key differs, try that track_info too
    if canonical_key != track_key:
        canon_info = existing_tracks.get(canonical_key, {})
        if isinstance(canon_info, dict):
            if not rel_path:
                rel_path = canon_info.get("source")
            track_info = canon_info
    
    # Fallback: resolve rel_path from file_paths
    if not rel_path:
        rel_path = _resolve_preview_rel_path(canonical_key, file_paths, preview_maps)
        if not rel_path:
            rel_path = file_paths.get(track_key)
        if not rel_path:
            # Try to match by filename
            for filename, path in file_paths.items():
                if track_key in filename or filename in track_key:
                    rel_path = path
                    break
    
    if not rel_path:
        raise HTTPException(400, detail=f"Could not determine source file path for track {track_key}")
    
    # Update track status to "queued"
    manifest_rel = f"previews/{_safe_track_folder(canonical_key)}/preview.m3u8"
    existing_tracks[canonical_key] = {
        "status": "queued",
        "manifest": manifest_rel,
        "error": None,
        "title_id": title_id,
        "source": rel_path,
    }
    if canonical_key != track_key:
        existing_tracks[track_key] = existing_tracks[canonical_key]
    
    # Calculate overall status
    if not existing_tracks:
        overall_status = "queued"
    elif all(v.get("status") == "completed" for v in existing_tracks.values()):
        overall_status = "completed"
    elif any(v.get("status") in ("queued", "running") for v in existing_tracks.values()):
        overall_status = "running"
    else:
        overall_status = "queued"
    
    previews = {
        "status": overall_status,
        "tracks": existing_tracks,
        "updated_at": datetime.datetime.utcnow().isoformat(),
    }
    user_reset_preview_auto_recovery_metadata(previews)
    disc_payload["previews"] = previews
    job.disc_payload = disc_payload
    db.commit()

    # Queue the single track preview generation task
    generate_preview_track.delay(job_id, canonical_key, rel_path, title_id=title_id)
    log.info("Job %s: Queued preview regeneration for track %s", job_id, canonical_key)
    
    return {"status": "queued", "track_key": canonical_key}


def _ripped_files_map_for_job(job: db_models.Job) -> dict[str, str]:
    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    if not post_paths and not ripped_files:
        dp = job.disc_payload or {}
        post_paths = dp.get("post_paths") or {}
        ripped_files = dp.get("ripped_files") or {}
    merged = post_paths if post_paths else ripped_files
    return dict(merged) if isinstance(merged, dict) else {}


def _title_needs_detection_rerun(
    tit: db_models.DiscTitle | None,
    *,
    missing_only: bool,
    force: bool,
) -> bool:
    if force:
        return True
    if not missing_only:
        return True
    if tit is None:
        return True
    meta = getattr(tit, "metadata_scan", None)
    if isinstance(meta, dict):
        w = meta.get("warning")
        if isinstance(w, str) and w.strip():
            return True
    if getattr(tit, "detection_flags", None) is None:
        return True
    return False


class DetectionRegenerateRequest(BaseModel):
    """Optional body for POST /jobs/{id}/detection/regenerate."""

    title_ids: Optional[List[str]] = None


@router.post("/{job_id}/detection/regenerate")
def regenerate_job_detection(
    job_id: str,
    db: Session = Depends(get_db),
    missing_only: bool = Query(True, description="If true (default), only titles with failed scan or no detection_flags."),
    force: bool = Query(False, description="If true, rerun for every title in scope regardless of missing_only."),
    body: Optional[DetectionRegenerateRequest] = Body(None),
):
    """
    Queue ffprobe metadata_scan + padding/junk detection for raw MKVs (detect_raw_titles).
    Scope: optional title_ids in body, else all titles in ripped_files for this job.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    if job.job_status in ("failed", "pending"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; detection regeneration not allowed")
    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed or skipped before regenerating detection")

    file_paths = _ripped_files_map_for_job(job)
    if not file_paths:
        raise HTTPException(400, detail="No post_paths or ripped_files found for this job")

    paths = JobPaths.from_job(job, out_dir=str(get_mkvauto_data()))
    raw_root = paths.raw
    scope_ids = list(body.title_ids) if body and body.title_ids else list(file_paths.keys())
    scope_ids = [str(t) for t in scope_ids if t]
    if not scope_ids:
        raise HTTPException(400, detail="No title IDs in scope")

    disc_id = getattr(job, "disc_id", None)
    title_by_id: dict[str, db_models.DiscTitle] = {}
    if disc_id:
        rows = (
            db.query(db_models.DiscTitle)
            .filter(
                db_models.DiscTitle.disc_id == disc_id,
                db_models.DiscTitle.id.in_(scope_ids),
            )
            .all()
        )
        title_by_id = {str(t.id): t for t in rows}

    queued: list[str] = []
    overrides: dict[str, str] = {}
    for tid in scope_ids:
        rel = file_paths.get(tid)
        if not rel:
            continue
        cand = (raw_root / rel).resolve()
        if not cand.exists():
            cand = (paths.root / rel).resolve()
        if not cand.exists():
            continue
        if not _title_needs_detection_rerun(title_by_id.get(tid), missing_only=missing_only, force=force):
            continue
        queued.append(tid)
        overrides[tid] = rel

    if not queued:
        raise HTTPException(
            400,
            detail="No titles to regenerate (check paths exist under raw/ or adjust missing_only/force)",
        )

    detect_raw_titles.delay(job_id, queued, rel_path_overrides=overrides or None)
    log.info("Job %s: queued detection regeneration for %d title(s)", job_id, len(queued))
    return {"status": "queued", "titles": queued, "count": len(queued)}


@router.post("/{job_id}/detection/regenerate/{title_id}")
def regenerate_job_detection_title(
    job_id: str,
    title_id: str,
    db: Session = Depends(get_db),
    force: bool = Query(True, description="Rerun detection for this title even if prior results exist."),
):
    """Queue detect_raw_titles for a single title (raw MKV must exist)."""
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    if job.job_status in ("failed", "pending"):
        raise HTTPException(400, detail=f"Job is {job.job_status}; detection regeneration not allowed")
    rip_state = getattr(job, "rip_state", None)
    if rip_state not in ("completed", "skipped"):
        raise HTTPException(400, detail="Rip must be completed or skipped before regenerating detection")

    file_paths = _ripped_files_map_for_job(job)
    rel = file_paths.get(title_id) or file_paths.get(str(title_id))
    if not rel:
        raise HTTPException(400, detail=f"No ripped_files entry for title {title_id}")

    paths = JobPaths.from_job(job, out_dir=str(get_mkvauto_data()))
    raw_root = paths.raw
    cand = (raw_root / rel).resolve()
    if not cand.exists():
        cand = (paths.root / rel).resolve()
    if not cand.exists():
        raise HTTPException(400, detail="Raw MKV not found on disk for this title")

    tit = None
    if getattr(job, "disc_id", None):
        tit = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.id == title_id, db_models.DiscTitle.disc_id == job.disc_id)
            .first()
        )
    if not force and tit and not _title_needs_detection_rerun(tit, missing_only=True, force=False):
        raise HTTPException(400, detail="Title does not need detection rerun; pass force=true to override")

    detect_raw_titles.delay(job_id, [str(title_id)], rel_path_overrides={str(title_id): rel})
    log.info("Job %s: queued detection regeneration for title %s", job_id, title_id)
    return {"status": "queued", "title_id": str(title_id)}


@router.delete("/{job_id}/previews")
def delete_previews(job_id: str, db: Session = Depends(get_db)):
    """
    Delete generated previews for a job and clear preview metadata.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    base = _job_base_dir(job)
    if base:
        try:
            shutil.rmtree(base / "previews", ignore_errors=True)
        except Exception as exc:
            raise HTTPException(500, detail=str(exc)) from exc
    disc_payload = job.disc_payload or {}
    disc_payload.pop("previews", None)
    job.disc_payload = disc_payload
    db.commit()
    return {"status": "deleted"}


@router.get("/{job_id}/track")
def stream_track(job_id: str, rel_path: str, db: Session = Depends(get_db)):
    """
    Serve a single .mkv file for preview. Uses rel_path relative to the job base dir.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    base = _job_base_dir(job)
    if not base:
        raise HTTPException(404, detail="No files available for this job")
    target = _rel_safe_path(base, rel_path)
    if not target.name.lower().endswith(".mkv"):
        raise HTTPException(400, detail="Only MKV files are allowed for preview")
    return FileResponse(target, media_type="video/x-matroska", filename=target.name)


@router.get("/{job_id}/label/prefill")
def prefill_label(job_id: str, db: Session = Depends(get_db)):
    """
    Build a best-effort label prefill from copy.log, disc_info.json, and mkv probes
    for discs that lack DiscDB metadata.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    base = _job_base_dir(job)
    if not base:
        raise HTTPException(404, detail="No files available for this job")

    payload = importbuddy_prefill.build_prefill(base)
    
    # Merge release information (including boxset_id) from the job's disc/release if available
    disc = getattr(job, "disc", None)
    rel = getattr(disc, "release", None) if disc else None
    if rel:
        # Include release information in the draft
        if not payload.get("release_id"):
            payload["release_id"] = str(rel.id)
        if not payload.get("release_slug"):
            payload["release_slug"] = rel.slug
        if not payload.get("release_name"):
            payload["release_name"] = rel.name
        if not payload.get("boxset_id"):
            payload["boxset_id"] = getattr(rel, "boxset_id", None)
    
    # label_draft holds only movie_id and group_type; do not persist release_* or boxset_* from prefill
    label_draft_only = {}
    if "movie_id" in payload:
        label_draft_only["movie_id"] = payload.get("movie_id")
    if "group_type" in payload:
        label_draft_only["group_type"] = payload.get("group_type")
    job.disc_payload = job.disc_payload or {}
    job.disc_payload["label_draft"] = label_draft_only
    if getattr(job, "disc", None):
        job.disc.label_draft = label_draft_only
    db.commit()
    db.refresh(job)
    return payload


@router.get("/by-disc", response_model=JobStatus)
def get_job_by_disc(
    disc_hash: str | None = None,
    disc_id: str | None = None,
    drive_num: str | None = None,
    disc_num: str | None = None,
    db: Session = Depends(get_db),
):
    """
    Return the most recent job matching a disc hash (preferred) or drive slot (drive_num).
    """
    slot = drive_num or disc_num
    if not disc_hash and not disc_id and not slot:
        # Avoid matching by drive slot alone to prevent reattaching to a previous disc's job.
        raise HTTPException(400, detail="disc_hash or disc_id is required")

    q = (
        db.query(crud.models.Job)  # type: ignore[attr-defined]
        .join(crud.models.Disc, crud.models.Job.disc_id == crud.models.Disc.id)  # type: ignore
    )
    if disc_id:
        q = q.filter(crud.models.Job.disc_id == disc_id)  # type: ignore
    elif disc_hash:
        q = q.filter(crud.models.Disc.content_hash == disc_hash)
    elif slot:
        q = q.filter(crud.models.Job.disc_num == str(slot))  # type: ignore
    job = q.order_by(crud.models.Job.created_at.desc()).first()  # type: ignore
    if not job:
        raise HTTPException(404, detail="Job not found for disc")

    return _build_job_status(job)




@router.get("/release/{release_slug}/progress", response_model=ReleaseProgress)
def release_progress(release_slug: str, db: Session = Depends(get_db)):
    """
    Return progress for a release: total discs, completed discs (latest job per disc completed),
    finalized discs (has finalize_result).
    """
    rel = (
        db.query(crud.models.Release)  # type: ignore[attr-defined]
        .filter(crud.models.Release.slug == release_slug)  # type: ignore
        .first()
    )
    if not rel:
        raise HTTPException(404, detail="Release not found")
    discs = rel.discs or []
    total = len(discs)
    completed = 0
    finalized = 0
    for disc in discs:
        if disc.finalize_result:
            finalized += 1
        if disc.jobs:
            latest_job = sorted(disc.jobs, key=lambda j: j.created_at)[-1]
            if latest_job.job_status == "completed":
                completed += 1
    return ReleaseProgress(
        release_slug=rel.slug,
        total_discs=total,
        completed_discs=completed,
        finalized_discs=finalized,
        finalize_state=rel.finalize_state,
    )


def _build_labelform_from_job(job: db_models.Job) -> Dict[str, Any]:
    """
    Build labelForm from job's disc_payload and label_payload.
    Mirrors frontend buildLabelForm logic.
    """
    disc_payload = job.disc_payload or {}
    label_payload = disc_payload.get("label_payload") or {}
    
    # Get disc object for fallback values
    disc = getattr(job, "disc", None)
    
    # Always prefer disc.label_draft when disc is available (source of truth for label fields)
    if disc and hasattr(disc, "label_draft"):
        stored_label_draft = disc.label_draft if isinstance(disc.label_draft, dict) else {}
    else:
        # Fallback to job.disc_payload.label_draft only if disc relationship not available
        stored_label_draft = disc_payload.get("label_draft") or {}
    
    label_draft = stored_label_draft
    
    # Merge label_draft into label_payload. label_draft holds movie_id, group_type, release_id, and boxset_id.
    merged_payload = {**label_payload, **label_draft}
    
    workflow_step = getattr(job, "workflow_step", None) or (
        label_draft.get("workflow_step") if isinstance(label_draft, dict) else None
    ) or _default_workflow_step(job)
    _group = merged_payload.get("group_type") or (disc_payload.get("title_type") or "movie").lower()
    _mode = merged_payload.get("mode") or (disc_payload.get("title_type") or "movie").lower()
    # Disc row is source of truth when loaded: never let stale label_payload overwrite DB (especially disc_number).
    if disc is not None:
        _disc_number_out = disc.disc_number
        if disc.disc_name is not None:
            _disc_name_out = disc.disc_name
        else:
            _disc_name_out = merged_payload.get("disc_name") or disc_payload.get("disc_name") or ""
        if disc.disc_slug is not None:
            _disc_slug_out = disc.disc_slug
        else:
            _disc_slug_out = merged_payload.get("disc_slug") or disc_payload.get("disc_slug") or ""
        _disc_format_out = disc.format or merged_payload.get("disc_format") or disc_payload.get("disc_format")
    else:
        _disc_number_out = merged_payload.get("disc_number")
        _disc_name_out = merged_payload.get("disc_name") or disc_payload.get("disc_name") or ""
        _disc_slug_out = merged_payload.get("disc_slug") or disc_payload.get("disc_slug") or ""
        _disc_format_out = merged_payload.get("disc_format") or disc_payload.get("disc_format")

    form: Dict[str, Any] = {
        "mode": _mode,
        "group_type": _group,
        "disc_group": merged_payload.get("disc_group") or merged_payload.get("release_slug") or "",
        "disc_number": _disc_number_out,
        "tmdb_id": merged_payload.get("tmdb_id") or "",
        "disc_format": _disc_format_out,
        "release_name": merged_payload.get("release_name") or "",
        "release_slug": merged_payload.get("release_slug") or merged_payload.get("disc_group") or "",
        "info_title": merged_payload.get("info_title") or disc_payload.get("info_title"),
        "upc": merged_payload.get("upc"),
        "asin": merged_payload.get("asin"),
        "cover_front_url": merged_payload.get("cover_front_url"),
        "cover_back_url": merged_payload.get("cover_back_url"),
        "release_year": merged_payload.get("release_year") or disc_payload.get("release_year"),
        "production_year": merged_payload.get("production_year") or disc_payload.get("production_year"),
        "disc_name": _disc_name_out,
        "disc_slug": _disc_slug_out,
        "movie_id": merged_payload.get("movie_id") if "movie_id" in label_draft else disc_payload.get("movie_id"),
        "boxset_id": merged_payload.get("boxset_id"),
        # Disc-card primary-season pick (#371). Persisted in label_draft so it
        # survives reload / websocket refetch; parity with
        # _build_labelform_from_disc. Defensive against bool / 0 / negative /
        # non-numeric values lurking in label_draft.
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
    
    # Add release fields if disc has release (disc already retrieved above).
    # Only apply disc.release when it belongs to the form's movie: if the user selected a different
    # movie (e.g. Rick and Morty) than the release's movie (e.g. Harry Potter), do not overwrite
    # release_id/boxset_id so the UI does not show the wrong release/boxset.
    # IMPORTANT: Prioritize label_draft values over disc.release to prevent state reversion,
    # BUT detect and fix stale label_draft (where draft conflicts with current database state).
    form_movie_id_before = merged_payload.get("movie_id")
    if disc and disc.release:
        rel = disc.release
        rel_movie_id = str(rel.movie_id) if rel.movie_id else None
        rel_id_str = str(rel.id)
        
        # Detect stale label_draft: if disc.release_id exists but label_draft has different/missing release_id
        label_draft_release_id = str(label_draft.get("release_id")) if label_draft.get("release_id") else None
        is_stale_release = (
            "release_id" in label_draft and 
            label_draft_release_id and 
            label_draft_release_id != rel_id_str
        )
        
        # Detect stale movie_id: if disc.release.movie_id exists but label_draft has different/missing movie_id
        label_draft_movie_id = str(label_draft.get("movie_id")) if label_draft.get("movie_id") else None
        is_stale_movie = (
            "movie_id" in label_draft and 
            label_draft_movie_id and 
            rel_movie_id and 
            label_draft_movie_id != rel_movie_id
        )
        
        # Apply release when: no movie selected yet (use disc.release to prefill) or release's movie matches form's movie
        form_movie_str = str(form_movie_id_before) if form_movie_id_before else None
        apply_release = (not form_movie_str) or (rel_movie_id and form_movie_str == rel_movie_id) or is_stale_movie
        
        # DEBUG: Log label_draft vs disc.release comparison
        logging.info(
            f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
            f"label_draft keys={list(label_draft.keys())}, "
            f"label_draft.movie_id={label_draft.get('movie_id')}, "
            f"label_draft.release_id={label_draft.get('release_id')}, "
            f"disc.release_id={disc.release_id}, "
            f"disc.release.movie_id={rel_movie_id}, "
            f"form_movie_str={form_movie_str}, "
            f"is_stale_release={is_stale_release}, "
            f"is_stale_movie={is_stale_movie}, "
            f"apply_release={apply_release}"
        )
        
        if not apply_release and form_movie_str:
            # Form has a different movie than disc.release; do not show that release/boxset.
            # Clear so UI does not show release/boxset from label_draft (wrong movie).
            logging.warning(
                f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                f"Movie mismatch detected! Clearing release_id/boxset_id. "
                f"label_draft.movie_id={form_movie_str} != disc.release.movie_id={rel_movie_id}"
            )
            form["release_id"] = None
            form["boxset_id"] = None
        if apply_release:
            # For stale release_id only: use disc.release as source of truth. For is_stale_movie only (user
            # selected a different movie/series), keep label_draft.movie_id so the response reflects the save.
            if is_stale_release:
                logging.warning(
                    f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                    f"Stale label_draft (release) detected! Using disc.release as source of truth."
                )
                form["release_id"] = rel_id_str
                form["movie_id"] = rel_movie_id
                if rel.boxset_id:
                    form["boxset_id"] = rel.boxset_id
            elif is_stale_movie:
                # User selected a new movie/series; keep their selection, clear mismatched release/boxset
                logging.info(
                    f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                    f"User selected different movie (label_draft.movie_id={label_draft_movie_id}); "
                    f"keeping it, not overwriting with disc.release.movie_id={rel_movie_id}"
                )
                form["release_id"] = None
                form["boxset_id"] = None
                # form["movie_id"] already set from label_draft at line 4776
            else:
                # Normal path: prioritize label_draft values to prevent reversion after rip completion
                if "release_id" not in label_draft:
                    form["release_id"] = rel_id_str
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"Applied release_id from disc.release: {rel.id}"
                    )
                elif label_draft_release_id == rel_id_str:
                    # Draft already matches DB; still set on labelForm so GET/PATCH merge is stable
                    # (initial form omits release_id from merged_payload).
                    form["release_id"] = rel_id_str
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"Applied release_id from label_draft (matches disc.release): {rel.id}"
                    )
                else:
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"NOT applying release_id from disc.release (release_id in label_draft). "
                        f"label_draft.release_id={label_draft.get('release_id')}, disc.release.id={rel.id}"
                    )
                # Only fill movie_id from release when we didn't get it from label_draft (explicit clear must not be overwritten)
                if rel.movie_id and "movie_id" not in label_draft:
                    form["movie_id"] = form["movie_id"] or rel.movie_id
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"Applied movie_id from disc.release: {rel.movie_id}"
                    )
                elif "movie_id" in label_draft:
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"NOT applying movie_id from disc.release (movie_id in label_draft). "
                        f"label_draft.movie_id={label_draft.get('movie_id')}, disc.release.movie_id={rel.movie_id}"
                    )
                # Prioritize label_draft boxset_id to prevent reversion
                if rel.boxset_id and "boxset_id" not in label_draft:
                    form["boxset_id"] = rel.boxset_id
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"Applied boxset_id from disc.release: {rel.boxset_id}"
                    )
                elif "boxset_id" in label_draft:
                    logging.info(
                        f"[DEBUG] _build_labelform_from_job - disc {disc.id}: "
                        f"NOT applying boxset_id from disc.release (boxset_id in label_draft). "
                        f"label_draft.boxset_id={label_draft.get('boxset_id')}, disc.release.boxset_id={rel.boxset_id}"
                    )
            
            # Always apply these fields from release (regardless of label_draft)
            form["release_slug"] = rel.slug
            form["disc_group"] = rel.slug
            # Prefer label_draft.group_type over rel.type so user's Movie/Series choice is not overwritten
            if "group_type" not in label_draft or not label_draft.get("group_type"):
                if getattr(rel, "type", None):
                    form["group_type"] = (rel.type or "movie").lower()
                    form["mode"] = form["group_type"]
            # Only backfill tmdb_id from linked release when form has no movie_id yet (avoid stale merged tmdb_id).
            if rel and rel.movie and rel.movie.tmdb_id and not form.get("movie_id"):
                form["tmdb_id"] = form["tmdb_id"] or rel.movie.tmdb_id
            if not form["release_name"]:
                form["release_name"] = rel.name or ""
            if not form["release_year"]:
                form["release_year"] = rel.release_year
            form["upc"] = form["upc"] or rel.upc
            form["asin"] = form["asin"] or rel.asin
            form["cover_front_url"] = form["cover_front_url"] or rel.cover_front_url
            form["cover_back_url"] = form["cover_back_url"] or rel.cover_back_url

    # When movie_id is set, tmdb_id must match that Movie row (ignore stale merged_payload / label_payload tmdb_id).
    _mid_sync = form.get("movie_id")
    if _mid_sync:
        _sess_sync = object_session(job)
        if _sess_sync:
            _movie_sync = _sess_sync.query(db_models.Movie).filter(db_models.Movie.id == _mid_sync).first()
            form["tmdb_id"] = (_movie_sync.tmdb_id if _movie_sync and _movie_sync.tmdb_id else "") or ""

    # When group_type was not in label_draft (defaults to "movie"), derive from selected Movie so toggle matches selection
    form_movie_id = form.get("movie_id")
    if form_movie_id and (form.get("group_type") or "movie").lower() == "movie":
        session = object_session(job)
        if session:
            movie = session.query(db_models.Movie).filter(db_models.Movie.id == form_movie_id).first()
            if movie and getattr(movie, "tmdb_type", None) == "tv":
                form["group_type"] = "series"
                form["mode"] = "series"

    # Add tracks/titles - prefer database records (source of truth), fall back to disc_payload
    # Prefer database records (they're updated when we save titles)
    if disc and disc.titles:
        seen_sources = set()
        for title in disc.titles:
            src = title.source_file or f"title_{title.id}"
            # Check if we already have this title (avoid duplicates)
            if src not in seen_sources:
                form["tracks"].append({
                    "source_file": src,
                    "track_id": src,
                    "title_id": title.id,  # Include title_id for frontend
                    "title_seq": title.title_seq,
                    "disc_track_id": None,
                    "title": title.title or "",
                    "description": title.description or "",
                    "note": title.description or "",  # back-compat field
                    "comment": title.comment,
                    "season": title.season,
                    "episode": title.episode,
                    "part": title.part,
                    "part_of": title.part_of,
                    "episode_end": title.episode_end,
                    "type": _normalize_title_type(title.type) or "",
                    "duration": title.duration,
                    "size": title.size,
                })
                seen_sources.add(src)
    
    # If no titles from database, fall back to disc_payload (legacy/initial state)
    if not form["tracks"]:
        titles = disc_payload.get("titles") or {}
        if titles:
            for src, title_data in titles.items():
                if isinstance(title_data, dict):
                    form["tracks"].append({
                        "source_file": src,
                        "track_id": src,
                        "title_id": title_data.get("title_id"),
                        "title_seq": title_data.get("title_seq"),
                        "disc_track_id": None,
                        "title": title_data.get("title") or "",
                        "description": title_data.get("description") or "",
                        "note": title_data.get("description") or "",
                        "comment": title_data.get("comment"),
                        "season": title_data.get("season"),
                        "episode": title_data.get("episode"),
                        "type": title_data.get("type") or "",
                        "duration": title_data.get("duration"),
                        "size": title_data.get("size"),
                    })
    
    # DEBUG: Log final form values
    if disc:
        logging.info(
            f"[DEBUG] _build_labelform_from_job - disc {disc.id}: Final form - "
            f"movie_id={form.get('movie_id')}, "
            f"release_id={form.get('release_id')}, "
            f"release_name={form.get('release_name')}, "
            f"release_slug={form.get('release_slug')}, "
            f"boxset_id={form.get('boxset_id')}, "
            f"workflow_step={form.get('workflow_step')}"
        )
    
    return form


def _get_titles_version_from_job(job: db_models.Job) -> int:
    disc = getattr(job, "disc", None)
    if disc and isinstance(disc.label_draft, dict):
        version = disc.label_draft.get("titles_version")
        if isinstance(version, int):
            return version
        if isinstance(version, str) and version.isdigit():
            return int(version)
    disc_payload = job.disc_payload or {}
    label_draft = disc_payload.get("label_draft") or {}
    version = label_draft.get("titles_version")
    if isinstance(version, int):
        return version
    if isinstance(version, str) and version.isdigit():
        return int(version)
    return 0


def _load_workflow_options_for_job(db: Session) -> Dict[str, Any]:
    """
    Load all options needed for workflow context (movies, boxsets, releases, groups).
    Same as disc workflow options.
    """
    from api.routers.discs import _load_workflow_options, _safe_disc_detail
    from core.disc_cache import get as cache_get
    return _load_workflow_options(db)


def _get_post_process_files(job: db_models.Job) -> List[Dict[str, Any]]:
    """
    Extract post-process files from job artifacts and final_paths.
    Mirrors frontend RippingService.getPostProcessFiles logic.
    """
    if not job:
        return []
    
    # Use post_paths (preferred) or ripped_files (fallback)
    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    file_paths = post_paths if post_paths else ripped_files
    per_title_status = getattr(job, "per_title_status", None) or {}
    artifacts = getattr(job.disc, "artifacts", None) if job.disc else (job.artifacts or {})
    
    post_process_files = []
    for title_id, path in file_paths.items():
        if not isinstance(path, str):
            continue
        
        # Determine status from per_title_status
        status: str = 'pending'
        title_status = per_title_status.get(title_id) if isinstance(per_title_status, dict) else None
        
        if title_status in ('completed', 'done'):
            status = 'completed'
        elif title_status in ('running', 'processing', 'active'):
            status = 'processing'
        elif path:
            # If in post_paths/ripped_files but no status, assume completed
            status = 'completed'
        
        file_info: Dict[str, Any] = {
            "name": title_id,
            "path": path,
            "status": status,
        }
        
        # Add progress from artifacts if available
        if isinstance(artifacts, dict) and title_id in artifacts:
            artifact_data = artifacts[title_id]
            if isinstance(artifact_data, dict) and "progress" in artifact_data:
                file_info["progress"] = artifact_data["progress"]
        
        post_process_files.append(file_info)
    
    return post_process_files


def _get_transfer_destination(job: db_models.Job, db: Session) -> Optional[Dict[str, Any]]:
    """
    Get transfer destination configuration from job or system settings.
    """
    # Check if job has transfer configuration
    if hasattr(job, "transfer_config") and job.transfer_config:
        return job.transfer_config
    
    # Fallback: get from system settings (would need to query settings table)
    # For now, return None if not in job
    return None


def _get_release_discs(release: db_models.Release, db: Session) -> List[Dict[str, Any]]:
    """
    Get all discs in a release.
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
    
    discs = (
        db.query(db_models.Disc)
        .options(joinedload(db_models.Disc.jobs), joinedload(db_models.Disc.release))
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
        
        # Build lightweight job summary instead of full _build_job_status() (which was ~110KB per disc).
        # The disc step and history page only need progress/state fields, not the full 80-field JobStatus.
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


def _get_boxset_movies(boxset: db_models.Boxset, db: Session) -> List[Dict[str, Any]]:
    """
    Get all movies in a boxset.
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


def _get_release_details(release: db_models.Release) -> Optional[Dict[str, Any]]:
    """
    Build full release details object.
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


@router.get("/{job_id}/workflow-context", response_model=WorkflowContextResponse)
def get_job_workflow_context(job_id: str, db: Session = Depends(get_db), *, _preloaded_job=None):
    """
    Get complete workflow context for a job.
    
    Args:
        job_id: Job ID (used for lookup if _preloaded_job is not provided)
        db: Database session
        _preloaded_job: Optional pre-loaded job object with eager-loaded relationships.
            When provided, skips the DB query (used by unfinished jobs batch endpoint).
    """
    # Import required functions for building discInfo
    from api.routers.discs import (
        _safe_disc_detail,
        _get_cached_context,
        _set_cached_context,
    )
    from core.disc_cache import get as cache_get
    from core.ffprobe_metadata import metadata_scan_to_summary as _metadata_scan_to_summary

    # 0. 10s TTL cache for the assembled response — same dict the disc-scoped
    #    endpoint already uses, keyed by `job:{id}`. Skipped when called from
    #    the unfinished-jobs batch loop (which passes a pre-loaded job and is
    #    a rare path) to avoid touching the same key from multiple call sites
    #    in one request. The cache is invalidated by
    #    `invalidate_workflow_context_cache(job_id=...)` on state changes.
    cache_key = f"job:{job_id}"
    if _preloaded_job is None:
        _cached = _get_cached_context(cache_key)
        if _cached is not None:
            return _cached

    # 1. Use pre-loaded job if provided, otherwise load with eager loading
    if _preloaded_job is not None:
        job = _preloaded_job
    else:
        # `Disc.titles` uses selectinload (NOT joinedload). joinedload would
        # multiply the title rows by the release+movie+boxset join breadth and
        # force psycopg2 to JSON-decode every `metadata_scan`/`streams`/
        # `chapters`/`playitem_durations_s` column on every title for every
        # workflow-context fetch — on Midway-class discs (222 titles) that
        # turns a single navigation into a 20–30s round-trip. selectinload
        # fires a separate IN-query that's roughly constant-time regardless of
        # join breadth. The disc-scoped endpoint already does this — see the
        # twin comment in `Backend/api/routers/discs.py` near line 1867.
        job = (
            db.query(db_models.Job)
            .options(
                joinedload(db_models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
                joinedload(db_models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
                joinedload(db_models.Job.disc).selectinload(db_models.Disc.titles),
            )
            .filter(db_models.Job.id == job_id)
            .first()
        )
    
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    # 2. Extract disc_payload and label_payload
    disc_payload = job.disc_payload or {}
    label_payload = disc_payload.get("label_payload") or {}
    
    # Extract disc_num from job record or disc_payload
    disc_num = getattr(job, 'disc_num', None) or disc_payload.get('disc_num')
    
    # 3. Build labelForm from label_payload
    labelForm = _build_labelform_from_job(job)
    from api.routers.discs import _workflow_pending_release_summary

    pending_release = (
        _workflow_pending_release_summary(db, job.disc) if getattr(job, "disc", None) else None
    )

    # 4. Extract titles - prefer database records (source of truth), merge scan metadata from disc_payload
    titles = []
    title_order = []

    def _build_payload_title_maps(payload_titles: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        by_title_id: dict[str, Any] = {}
        by_source: dict[str, Any] = {}
        if isinstance(payload_titles, dict):
            items = payload_titles.items()
        elif isinstance(payload_titles, list):
            items = [(t.get("title_id"), t) for t in payload_titles if isinstance(t, dict)]
        else:
            items = []
        for key, title_data in items:
            if not isinstance(title_data, dict):
                continue
            title_id = title_data.get("title_id") or title_data.get("id") or key
            if title_id:
                by_title_id[str(title_id)] = title_data
            source_key = title_data.get("source_file") or title_data.get("file") or key
            if source_key:
                by_source[str(source_key)] = title_data
        return by_title_id, by_source

    payload_titles_data = disc_payload.get("titles") or {}
    payload_by_title_id, payload_by_source = _build_payload_title_maps(payload_titles_data)
    preserve_fields = [
        "chapters",
        "streams",
        "duration",
        "duration_raw",
        "duration_seconds",
        "size",
        "display_size",
        "file",
        "src",
        "source_file",
        "track_id",
        "title_id",
        "title_seq",
        "edition",
        "output_file",
        "output_path",
        "playlist",
        "playlist_index",
        "segment_map",
        "segmentMap",
        "name",
        "index",
        "order_index",
        "comment",
        "language_code",
        "language",
        "detection_flags",
        "detection_confidence",
        "detection_warning",
    ]

    def _merge_payload_metadata(title_obj: dict, payload_title: dict | None) -> dict:
        if not payload_title:
            return title_obj
        for field in preserve_fields:
            existing = title_obj.get(field)
            incoming = payload_title.get(field)
            if (existing is None or existing == "") and incoming is not None:
                title_obj[field] = incoming
        return title_obj

    def _find_payload_for_db(title_record: db_models.DiscTitle) -> dict | None:
        if title_record.id and str(title_record.id) in payload_by_title_id:
            return payload_by_title_id[str(title_record.id)]
        if title_record.source_file and str(title_record.source_file) in payload_by_source:
            return payload_by_source[str(title_record.source_file)]
        return None

    # Prefer database records (they're updated when we save titles)
    if job.disc and job.disc.titles:
        for title in job.disc.titles:
            title_id = str(title.id)
            # Check if we already have this title (avoid duplicates)
            if title_id not in title_order:
                meta = getattr(title, "metadata_scan", None)
                title_obj = {
                    "src": title_id,
                    "source_file": title.source_file,
                    "title_id": title.id,
                    "title_seq": title.title_seq,
                    "segment_map": getattr(title, "segment_map", None),
                    "index": title.index,
                    "order_index": title.order_index,
                    "title": title.title,
                    "edition": getattr(title, "edition", None),
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
                    "metadata_scan": meta,
                    # Pre-computed summary lines for the title-editor tooltip
                    # (quality/audio/subtitle tier strings). Used to live on
                    # the redundant `disc_info["titles"]` dict the response
                    # also carried; we dropped that dict for payload size and
                    # now compute the summary once per top-level title row.
                    "metadata_summary": _metadata_scan_to_summary(meta) if meta else None,
                    "detection_flags": getattr(title, "detection_flags", None),
                    "detection_confidence": getattr(title, "detection_confidence", None),
                    "detection_warning": getattr(title, "detection_warning", None),
                    # Path B labeling dedupe surfaces these to the frontend.
                    "obfuscation_flag": bool(getattr(title, "obfuscation_flag", False)),
                    # Tier-aware decoy reason ('segment_set_sibling' / 'path_a_decoy'
                    # = HIGH, 'makemkv_msg3307' = MEDIUM, NULL = not flagged). Drives
                    # the visual tier of the "Likely decoy" badge on the titles UI.
                    "obfuscation_reason": getattr(title, "obfuscation_reason", None),
                    # When this row's clip ID is wrapped by another title's
                    # segment_map on the same disc, this UUID points to that
                    # wrapper. Phase 6 onward writes this; until then it stays
                    # NULL and the frontend treats every row as standalone.
                    "subsumed_by_title_id": getattr(title, "subsumed_by_title_id", None),
                    # Source-split of every label field for the titles-step
                    # chip system: auto_* = automated detection (DiscDB,
                    # scan-time, Path A sibling-ignore, subsumption, etc);
                    # user_* = direct user input. The effective resolved
                    # columns (user ?? auto) are preserved above.
                    **crud.title_provenance_payload(title),
                    "force_independent_group": bool(getattr(title, "force_independent_group", False)),
                    "playitem_durations_s": getattr(title, "playitem_durations_s", None),
                }
                payload_title = _find_payload_for_db(title)
                title_obj = _merge_payload_metadata(title_obj, payload_title)
                titles.append(title_obj)
                title_order.append(title_id)

    # If no titles from database, fall back to disc_payload (legacy/initial state)
    if not titles and payload_titles_data:
        if isinstance(payload_titles_data, dict):
            items = payload_titles_data.items()
        elif isinstance(payload_titles_data, list):
            items = [(t.get("title_id"), t) for t in payload_titles_data if isinstance(t, dict)]
        else:
            items = []
        for title_id, title_data in items:
            if isinstance(title_data, dict):
                if not title_id:
                    continue
                title_obj = {
                    "src": str(title_id),
                    "source_file": title_data.get("source_file"),
                    "title_id": title_id,
                    **title_data,
                }
                titles.append(title_obj)
                title_order.append(str(title_id))

    # Attach duplicate_info (existing order-preserving grouping) to response titles.
    dedupe_groups: list[dict] = []
    if job.disc and titles:
        titles_by_id_ref = {str(t.get("title_id") or t.get("src")): t for t in titles}
        attach_duplicate_info(titles_by_id_ref, str(job.disc.id))

        # Path B sorted-segment-set dedupe: parallel grouping that captures
        # the Midway-class case order-preserving grouping misses. Annotate
        # each title with its dedupe_group_id and surface the groups (with
        # representative + disagreement metadata) on the workflow context.
        #
        # PURE COMPUTE ONLY. The persisting half (obfuscation_reason +
        # subsumption marks) used to run right here, committing mid-GET —
        # which made this response stale relative to its own side effects
        # and turned read traffic into write churn (~300 rows re-stamped on
        # the first GET after a restart, measured on the rc rig). It now
        # runs where the inputs change: scan ingest
        # (crud._apply_path_b_marks_for_disc_safe) and detect completion
        # (workers/preview_detect_phases). This GET must never write.
        from core.path_b_dedupe import (
            annotate_titles_with_dedupe_group as _annotate_dedupe,
            compute_dedupe_groups as _compute_dedupe,
            compute_mpls_clip_index as _compute_clip_index,
            fold_subsumption_into_groups as _fold_subsumption,
        )
        clip_index = _compute_clip_index(titles_by_id_ref)
        groups_path_b = _compute_dedupe(titles_by_id_ref)
        # The fold runs on the response annotation only — component clips
        # collapse into their wrapper's group in the left rail. The
        # persisted marks were applied (in the same order: reason before
        # fold, subsumption after) at scan/detect time.
        groups_path_b = _fold_subsumption(groups_path_b, clip_index, titles_by_id_ref)
        _annotate_dedupe(titles_by_id_ref, groups_path_b)
        dedupe_groups = [g.to_dict() for g in groups_path_b]

    # 5. Options are NOT loaded here - frontend fetches them separately via GET /discs/options
    
    # 6. Build job status (this includes movie_name from release.movie.name)
    job_status = _build_job_status(job)
    
    # 6.5. Get related data (post-process files, transfer destination, release discs, boxset movies, release details)
    post_process_files = _get_post_process_files(job)
    transfer_destination = _get_transfer_destination(job, db)
    
    release_discs = []
    boxset_movies = []
    last_release_details = None
    
    if job.disc and job.disc.release:
        rel = job.disc.release
        release_discs = _get_release_discs(rel, db)
        last_release_details = _get_release_details(rel)
        
        # Get boxset movies if release is in a boxset
        if rel.boxset_id:
            boxset = db.query(db_models.Boxset).filter(db_models.Boxset.id == rel.boxset_id).first()
            if boxset:
                boxset_movies = _get_boxset_movies(boxset, db)
    
    # 7. Build discInfo from job.disc (unified with disc contexts)
    disc_detail = None
    if job.disc:
        disc = job.disc
        rel = getattr(disc, "release", None)
        # Build disc_info dict from disc record (similar to disc contexts).
        # Use job's release for cover so we never show the currently-inserted disc's image.
        disc_info = {
            "disc_id": str(disc.id),
            "disc_num": disc_num or "unknown",
            "mount_point": job.mount_point or "unknown",
            "disc_hash": disc.content_hash,
            "disc_name": disc.disc_name,
            "disc_slug": disc.disc_slug,
            "disc_format": disc.format,
            "disc_number": disc.disc_number,
            "release_image": getattr(rel, "cover_front_url", None) if rel else disc_payload.get("release_image"),
        }
        
        # Try to get additional info from cache only when it refers to THIS disc.
        # Cache keyed by disc_num holds the currently-inserted disc, so do not use it for jobs.
        cached = None
        if disc.content_hash:
            try:
                cached = cache_get(str(disc.content_hash))
            except Exception:
                pass  # Ignore cache lookup errors
        if not cached and disc_num and disc_num != "unknown":
            try:
                c = cache_get(str(disc_num))
                # Only use cache if it is for the same disc (same disc_id or content_hash)
                if c and (
                    c.get("disc_id") == str(disc.id)
                    or (c.get("content_hash") or c.get("disc_hash")) == disc.content_hash
                ):
                    cached = c
            except Exception:
                pass  # Ignore cache lookup errors
        
        if cached:
            # Merge cached data (may have more complete info); then re-apply job's release_image
            disc_info.update(cached)
            if disc_num:
                disc_info["disc_num"] = disc_num
            elif cached.get("disc_num"):
                disc_info["disc_num"] = cached.get("disc_num")
            disc_info["mount_point"] = job.mount_point or cached.get("mount_point") or "unknown"
            # Always keep the job's own release image (never show currently-inserted disc's cover)
            job_release_image = getattr(rel, "cover_front_url", None) if rel else disc_payload.get("release_image")
            if job_release_image is not None:
                disc_info["release_image"] = job_release_image

        # Note: we used to also build `disc_info["titles"]` here as a dict
        # keyed by title_id — duplicating the top-level `titles` array the
        # response already carries. On a 222-title disc that was ~180 KB of
        # the ~520 KB response, plus a second `attach_duplicate_info` walk
        # over every title. The frontend's `_convertApiResponseToContext`
        # parser reads the top-level `titles` array, not `discInfo.titles`
        # (the only reader, `buildContextFromDisc`, is dead code), so we
        # drop the redundancy here. `metadata_summary` is now computed
        # alongside `metadata_scan` on each top-level title row above.

        # Build DiscDetail using _safe_disc_detail
        disc_detail = _safe_disc_detail(disc_info, disc_num or "unknown", job.mount_point or "unknown")
    
    # 8. Get movie/release metadata for workflow context
    # Prioritize Movie database record, fallback to info_title if no Movie linked
    movie_name = None
    movie_cover = None
    production_year = None
    if job.disc:
        disc = job.disc
        rel = getattr(disc, "release", None)
        movie = getattr(rel, "movie", None) if rel else None
        
        if movie:
            # Use Movie database record
            movie_name = movie.name
            movie_cover = movie.cover_url
            production_year = movie.production_year
        else:
            # No Movie linked - use info_title like the cards do
            movie_name = disc.info_title if disc.info_title else disc_payload.get("info_title")
            movie_cover = None  # No cover when using info_title
            production_year = None  # No production year when using info_title
    
    titles_version = _get_titles_version_from_job(job)

    # 9. Build response, cache for 10s, return.
    response = WorkflowContextResponse(
        id=job_id,
        type="job",
        discId=str(job.disc.id) if job.disc else None,
        mountPoint=job.mount_point,
        discNum=disc_num,
        labelForm=labelForm,
        titles=titles,
        titleOrder=title_order,
        titlesVersion=titles_version,
        jobStatus=job_status,
        discInfo=disc_detail,  # Include discInfo for frontend to extract disc_id
        movieOptions=[],  # Loaded separately via GET /discs/options
        boxsetOptions=[],
        releaseOptions=[],
        pendingRelease=pending_release,
        groupOptions=[],
        labelDraftProcessed=bool(disc_payload.get("label_draft")),
        discNameLocked=bool(labelForm.get("disc_name")),
        discSlugLocked=bool(labelForm.get("disc_slug")),
        isSeries=(disc_payload.get("title_type") or "").lower() == "series",
        discdbHit=_workflow_discdb_hit_for_context(job, disc_payload),
        discdb_result=getattr(job, "discdb_result", None),
        discMode=job.mode or "copy",
        lastReleaseDetails=last_release_details,
        releaseNameHint=disc_payload.get("movie_name") or "",
        releaseSlugHint="",
        postProcessFiles=post_process_files,
        transferDestination=transfer_destination,
        releaseDiscs=release_discs,
        boxsetMovies=boxset_movies,
        movieCover=movie_cover,
        movieName=movie_name,
        productionYear=production_year,
        dedupeGroups=dedupe_groups,
    )
    if _preloaded_job is None:
        # Only cache the single-job endpoint path; the batch endpoint passes
        # `_preloaded_job` and would otherwise stomp the cache key on every
        # iteration of its loop.
        _set_cached_context(cache_key, response)
    return response


@router.post("/{job_id}/finish", status_code=204)
def finish_job(job_id: str, db: Session = Depends(get_db)):
    """
    Mark a job as completed (dismiss from carousel).

    Allowed only when the job is in a "done" state: phase is complete and
    transfer is completed, with job_status still running or validating.
    Transitions job_status to "completed", which triggers job_finished on the
    coordinator so the frontend removes the job from the card carousel.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")

    phase = getattr(job, "phase", None) or ""
    transfer_state = getattr(job, "transfer_state", None) or ""
    job_status = (getattr(job, "job_status", None) or "").lower()

    # Idempotent: already completed (e.g. after first successful finish) -> 204
    if job_status == "completed":
        return Response(status_code=204)

    if job_status not in ("running", "validating"):
        raise HTTPException(
            400,
            detail=f"Job is {job_status}; finish only allowed when job is running or validating",
        )
    if phase != "complete":
        raise HTTPException(
            400,
            detail=f"Job phase is {phase!r}; finish only allowed when phase is complete",
        )
    if transfer_state != "completed":
        raise HTTPException(
            400,
            detail=f"Transfer state is {transfer_state!r}; finish only allowed when transfer is completed",
        )

    # Build updates so the completed invariant passes (label/finalize/finalize_release may be unset).
    updates: dict = {"job_status": "completed"}
    profile = _infer_profile(job)
    default_terminal = "skipped" if profile == "hit" else "completed"
    for field in ("label_state", "finalize_state", "finalize_release_state"):
        cur = (getattr(job, field, None) or "").strip().lower()
        if cur not in ("completed", "skipped"):
            updates[field] = default_terminal

    try:
        apply_job_state(
            db,
            job,
            updates=updates,
            reason="user finished (dismiss from carousel)",
        )
    except StateViolation as exc:
        raise HTTPException(409, detail=str(exc)) from exc

    log.info("Job %s: Marked as completed (user finish)", job_id)
    if JobPaths.for_id(str(job.id)).root.exists():
        cleanup_job_mkv.delay(str(job.id), "user_finish")

    # Eject on finish (#20): if enabled, physically eject the disc after job completion
    try:
        from core.settings import load_settings
        if load_settings().get("eject_on_finish"):
            mount_point = getattr(job, "mount_point", None)
            if not mount_point and job.disc:
                # Try to find mount_point from cached discs
                from core.disc_cache import get_cached_discs
                for cached in get_cached_discs():
                    if cached.get("disc_id") == str(job.disc.id):
                        mount_point = cached.get("mount_point")
                        break
            if mount_point:
                from core.utils import eject_disc
                eject_disc(mount_point)
            else:
                log.debug("Job %s: eject_on_finish enabled but no mount_point found for disc", job_id)
    except Exception as exc:
        log.warning("Job %s: eject_on_finish failed: %s", job_id, exc)
    return Response(status_code=204)



@router.get("/unfinished/summaries")
def get_unfinished_job_summaries(db: Session = Depends(get_db)):
    """
    Lightweight summaries for unfinished job cards (card carousel).
    
    Returns only the fields needed to render a card (~500 bytes per job vs ~960KB
    for the full workflow context). Full context is loaded on-demand when the user
    clicks a card (via GET /jobs/{id}/workflow-context).
    """
    from api.unfinished_jobs import query_unfinished_jobs
    unfinished_jobs = query_unfinished_jobs(db)

    summaries = []
    for job in unfinished_jobs:
        disc = getattr(job, "disc", None)
        rel = getattr(disc, "release", None) if disc else None
        movie = getattr(rel, "movie", None) if rel else None
        summaries.append({
            "job_id": str(job.id),
            "disc_id": str(disc.id) if disc else None,
            "disc_hash": disc.content_hash if disc else None,
            "job_status": job.job_status,
            "rip_progress": job.rip_progress,
            "post_progress": getattr(job, "post_progress", 0),
            "transfer_progress": getattr(job, "transfer_progress", None),
            "rip_state": getattr(job, "rip_state", None),
            "post_state": job.derived_post_state,  # #365 — derived, not column
            "transfer_state": getattr(job, "transfer_state", None),
            "workflow_step": getattr(job, "workflow_step", None),
            "stage_profile": getattr(job, "stage_profile", None),
            "movie_name": movie.name if movie else (disc.info_title if disc else None),
            "release_name": rel.name if rel else None,
            "disc_format": disc.format if disc else None,
            "disc_number": disc.disc_number if disc else None,
            "release_image": rel.cover_front_url if rel else None,
            "production_year": movie.production_year if movie else None,
            "release_year": rel.release_year if rel else None,
            "resolution": rel.resolution if rel else None,
            "mount_point": job.mount_point,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "discdb_result": getattr(job, "discdb_result", None),
        })
    
    return summaries


@router.get("/unfinished/workflow-contexts", response_model=List[WorkflowContextResponse])
def get_unfinished_jobs_workflow_contexts(db: Session = Depends(get_db)):
    """
    Get workflow contexts for all unfinished jobs.
    A job is unfinished if:
    - rip_state is 'completed' or 'skipped' (copy/rip finished)
    - job_status is 'running' (job is still active, not completed or failed)
    This endpoint is called on frontend startup to build the cache.
    Note: _cleanup_stale_jobs is not run here to avoid holding the DB session during
    Celery inspect/AsyncResult checks; that led to pool exhaustion (502s) when many
    requests hit in parallel on page load. Cleanup still runs from other job endpoints.
    """
    # Query unfinished jobs
    # A job is unfinished if:
    # - rip_state is 'completed' or 'skipped' (copy/rip finished)
    # - job_status is 'running' or 'validating' (job is active, not completed or failed)
    # Note: We exclude job_status='completed' and 'failed' to match frontend expectations
    unfinished_jobs = (
        db.query(db_models.Job)
        .options(
            joinedload(db_models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.movie),
            joinedload(db_models.Job.disc).joinedload(db_models.Disc.release).joinedload(db_models.Release.boxset),
            # selectinload, not joinedload — see the Cartesian-product comment
            # on the single-job workflow-context query above. This batch
            # endpoint is even more sensitive: it loads every unfinished job,
            # so the multiplier is per-job × 222-titles.
            joinedload(db_models.Job.disc).selectinload(db_models.Disc.titles),
        )
        .filter(
            db_models.Job.rip_state.in_(["completed", "skipped"]),
            db_models.Job.job_status.in_(["running", "validating"]),
        )
        .all()
    )
    # Build contexts for all unfinished jobs using pre-loaded job objects.
    # Pass _preloaded_job to avoid N additional DB queries (one per job).
    # Options are NOT loaded (Phase 1) — frontend fetches separately via GET /discs/options.
    contexts = []
    for job in unfinished_jobs:
        try:
            context = get_job_workflow_context(str(job.id), db, _preloaded_job=job)
            contexts.append(context)
        except Exception as e:
            log.warning("Failed to build workflow context for job %s: %s", job.id, e, exc_info=True)
    
    return contexts


@router.put("/{job_id}/workflow-context", response_model=WorkflowContextResponse)
def save_job_workflow_context(
    job_id: str,
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(get_db)
):
    """
    Save complete labelForm for a job. Persists label_draft and applies to disc/release/track
    records only. Does not perform phase or state transitions; use POST /jobs/{id}/label/complete
    for completing the label stage (titles -> postprocess).
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    
    disc = getattr(job, "disc", None)
    if not disc:
        raise HTTPException(400, detail="Job has no disc attached")
    
    # Convert labelForm to LabelRequest format (or use directly)
    lp = update.labelForm.copy()
    _strip_stale_release_hints_if_movie_cleared(lp)
    # Save workflow_step on the job; other draft fields stay in label_draft in job.disc_payload
    # This is separate from _apply_label_to_records which only saves to disc/release/track records
    disc_payload = job.disc_payload or {}
    label_draft = disc_payload.get("label_draft") or {}
    
    if lp.get("workflow_step"):
        incoming_step = lp["workflow_step"]
        phase = getattr(job, "phase", None)
        current_step = getattr(job, "workflow_step", None)
        if phase in ("postprocess", "transfer", "finalize_release", "complete") or current_step in ("postprocess", "transfer"):
            pass  # Ignore incoming workflow_step when in postprocess/transfer
        else:
            order = _step_order(job)
            cur_idx = order.index(current_step) if current_step in order else -1
            inc_idx = order.index(incoming_step) if incoming_step in order else -1
            if cur_idx >= 0 and inc_idx >= 0 and inc_idx < cur_idx:
                # Stale client (e.g. UI-only back) must not regress persisted step
                lp.pop("workflow_step", None)
            else:
                job.workflow_step = incoming_step
    # label_draft holds movie_id, group_type, release_id, and boxset_id for state persistence
    if "group_type" in lp:
        v = lp.get("group_type")
        label_draft["group_type"] = (v or "movie").lower() if v else None
    if "movie_id" in lp:
        label_draft["movie_id"] = lp["movie_id"]
    # Store release_id and boxset_id to prevent state reversion after rip completion
    if "release_id" in lp:
        label_draft["release_id"] = lp["release_id"]
    if "boxset_id" in lp:
        label_draft["boxset_id"] = lp["boxset_id"]
    # Keep only allowed keys for label_draft
    allowed_label_draft_keys = {"movie_id", "group_type", "release_id", "boxset_id"}
    label_draft = {k: v for k, v in label_draft.items() if k in allowed_label_draft_keys}

    # Save updated label_draft back to disc_payload and to disc.label_draft (so _build_labelform_from_job sees it)
    disc_payload["label_draft"] = label_draft
    job.disc_payload = disc_payload
    disc.label_draft = dict(label_draft)

    # Force SQLAlchemy to persist JSON column changes (in-place dict mutation is not detected)
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(job, "disc_payload")
    flag_modified(disc, "label_draft")

    # Title rows are NOT written from this endpoint. Autosave of the label
    # form must never carry a titles payload: title edits go through
    # PATCH /discs/{id}/titles, which is the only path with optimistic
    # concurrency (base_seq / If-Match), per-title write serialization and
    # field provenance.
    #
    # The client already strips `tracks` before sending (#349) — but the
    # PATCH variant of this endpoint merges the incoming form over the
    # SERVER's current labelForm, and that one carries `tracks` for every
    # title on the disc. The merge silently put them back, so each autosave
    # rewrote all of them via _apply_label_to_records with source="user",
    # from a snapshot read at the start of the request.
    #
    # Measured on rc.3: a labeling session issued 10 of these and rewrote
    # all 302 rows of one disc in a single timestamp. Any title edit landing
    # after that snapshot was read — or still in flight — was overwritten by
    # the older value. That is the "my edit reverted" report, and a snapshot
    # caught mid-typing is why titles came back truncated ("Hulk Chases T").
    #
    # save_label / complete_label still pass titles deliberately; only this
    # autosave path is stripped.
    lp.pop("tracks", None)
    lp.pop("titles", None)

    # Call existing label save logic (saves to disc/release/track records)
    _apply_label_to_records(disc, lp, db)
    _sync_job_disc_payload_disc_label_fields(job, disc)

    db.commit()
    db.refresh(job)
    db.refresh(disc)

    # Invalidate cached workflow context so the rebuilt response below reads
    # fresh state. Without this, a subsequent GET (including the one issued by
    # PATCH for merge) could return a pre-save cached snapshot — e.g. an
    # explicit movie_id=None clear would echo the previous movie_id back.
    from api.routers.discs import invalidate_workflow_context_cache
    invalidate_workflow_context_cache(job_id=job_id, disc_id=disc.id)

    # Return updated workflow context
    context = get_job_workflow_context(job_id, db)
    
    # Emit websocket notification after successful save
    try:
        from api.routers.websockets import _emit_to_job_workflow
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            asyncio.create_task(_emit_to_job_workflow(job_id, changed_fields=['labelForm']))
        except RuntimeError:
            # No running loop - try to get app reference
            try:
                from api.main import _app_instance
                if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                    loop = _app_instance.state.event_loop
                    asyncio.run_coroutine_threadsafe(_emit_to_job_workflow(job_id, changed_fields=['labelForm']), loop)
            except Exception as exc:
                log.warning(f"Failed to schedule websocket emission for job {job_id}: {exc}")
    except Exception as exc:
        log.warning(f"Failed to emit workflow context change notification to websocket for job {job_id}: {exc}")
    
    return context


@router.patch("/{job_id}/workflow-context", response_model=WorkflowContextResponse)
def update_job_workflow_context(
    job_id: str,
    update: WorkflowContextUpdate = Body(...),
    db: Session = Depends(get_db)
):
    """
    Partially update labelForm for a job (for auto-save).
    """
    # For PATCH, merge with existing labelForm before applying
    # Get current context
    current_context = get_job_workflow_context(job_id, db)
    current_labelForm = current_context.labelForm or {}
    
    # Merge updates
    merged_labelForm = {**current_labelForm, **update.labelForm}
    
    # Use merged form for save
    update.labelForm = merged_labelForm
    return save_job_workflow_context(job_id, update, db)


# Allowed (from_step, to_step) for workflow/step/complete. film->boxset requires rip running/completed.
_STEP_COMPLETE_ALLOWED = {
    ("film", "boxset"),
    ("boxset", "disc"),
    ("disc", "titles"),
    ("postprocess", "transfer"),
    ("summary", "postprocess"),
}

# Step order for "backward" (set step only) detection.
_STEP_ORDER_MISS = ("film", "boxset", "disc", "titles", "postprocess", "transfer")
_STEP_ORDER_HIT = ("summary", "postprocess", "transfer")


def _step_order(job) -> tuple:
    from core.job_state import _infer_profile
    profile = _infer_profile(job)
    return _STEP_ORDER_HIT if profile == "hit" else _STEP_ORDER_MISS


def _is_backward(to_step: str, current: str, order: tuple) -> bool:
    try:
        return order.index(to_step) < order.index(current)
    except ValueError:
        return False


@router.post("/{job_id}/workflow/step/complete", response_model=JobStatus)
def complete_workflow_step(
    job_id: str,
    body: StepCompleteRequest,
    db: Session = Depends(get_db),
):
    """
    Advance workflow_step only (no stage/phase changes). Frontend applies the returned
    JobStatus (workflow_step, jobStatus) and does not rely on WebSocket for this transition.
    """
    job = crud.get_job(db, job_id)
    if not job:
        raise HTTPException(404, detail="Job not found")
    disc = getattr(job, "disc", None)
    if not disc:
        raise HTTPException(400, detail="Job has no disc attached")

    current = getattr(job, "workflow_step", None)
    phase = getattr(job, "phase", None)
    if not current:
        if phase in ("postprocess", "transfer", "finalize_release", "complete"):
            current = "postprocess" if phase == "postprocess" else "transfer"
        else:
            current = _default_workflow_step(job)

    # Allowed forward: apply film->boxset rip check if needed, then set step
    # Release creation/linking happens when the user selects a movie (POST /discs/{id}/releases), not here.
    if (current, body.to_step) in _STEP_COMPLETE_ALLOWED:
        if (current, body.to_step) == ("film", "boxset"):
            rip = getattr(job, "rip_state", None) or (getattr(job, "pipeline") or {}).get("rip")
            if rip not in ("running", "completed"):
                raise HTTPException(
                    400,
                    detail="film->boxset requires rip_state running or completed",
                )
        job.workflow_step = body.to_step
        db.commit()
        db.refresh(job)
        return _build_job_status(job)

    # Backward: set step only, no phase/stage changes
    order = _step_order(job)
    if _is_backward(body.to_step, current, order):
        job.workflow_step = body.to_step
        db.commit()
        db.refresh(job)
        return _build_job_status(job)

    raise HTTPException(
        400,
        detail={
            "message": f"Invalid step transition from {current!r} to {body.to_step!r}",
            "current_step": current,
        },
    )


# ════════════════════════════════════════════════════════════════════════════
# Path A — segment-reorder endpoints (Phase 2)
# ════════════════════════════════════════════════════════════════════════════
#
# Triggered from the frontend modal after the /jobs/rip 409 needs_user_choice
# response. Three endpoints land here:
#
#   POST /jobs/rip-with-segment-reorder    create the Path A job + start
#                                           exploratory rip of one playlist
#   POST /jobs/{id}/segment-order          accept user's segment ordering,
#                                           match against disc playlists
#   POST /jobs/{id}/segment-reorder/cancel bail out of Path A, clean up
#

class SegmentReorderStartReq(BaseModel):
    """Body for POST /jobs/rip-with-segment-reorder.

    `exploratory_title_index` is OPTIONAL. Within a duplicate-segment-map
    group every member references the same underlying segments, so the
    choice is a backend concern — when omitted, we re-evaluate the Path A
    gate and use its auto-pick (DiscDB classification > MakeMKV flag-clear
    > first member of the largest group). Tests / power users can still
    pass a specific index to override.
    """
    mount_point: str
    disc_id: Optional[str] = None
    disc_num: Optional[str] = None
    exploratory_title_index: Optional[int] = None
    output_dir: Optional[str] = None
    # #578: bypass the USB-bus-saturation gate after the user has
    # acknowledged the bandwidth contention warning in the UI. Mirrors
    # the same field on JobCreate.
    force_concurrent_on_saturated_bus: Optional[bool] = False


class SegmentOrderSubmitReq(BaseModel):
    """Body for POST /jobs/{id}/segment-order. `order` is the user-chosen
    segment ordering as a list of segment IDs (clip names from the previews).
    """
    order: List[str]


class SegmentOrderConfirmReq(BaseModel):
    """Body for POST /jobs/{id}/segment-order/confirm. Sent after the user
    answers "yes, my order is correct" on the no-match confirmation gate.

    Marks `confirmed_segment_order` on the job state so downstream
    iteration steps (flag-decoys, rip-the-rest) know the order has been
    user-validated. The matcher reruns with the disc's per-clip flags so
    subsequence-superset candidates are surfaced.
    """
    order: List[str]


class SegmentOrderFlagDecoysReq(BaseModel):
    """Body for POST /jobs/{id}/segment-order/flag-decoys. Sent when the
    user concludes the most-recent exploratory rip targeted a decoy
    playlist — either because no candidate matches or because review of
    the candidate's previews revealed the wrong content.

    The endpoint marks the exploratory mpls AND every sibling mpls
    sharing its sorted-segment-set as `type='ignore'` so the iteration
    loop won't pick the same cluster again. Wipes `submitted_order`,
    appends to `eliminated_title_indexes`, and bumps the iteration
    history with `outcome='flagged_decoys'`.
    """
    exploratory_title_index: int


class SegmentOrderRipSupersetReq(BaseModel):
    """Body for POST /jobs/{id}/segment-order/rip-superset. Sent when the
    user picks a subsequence-superset candidate from the picker modal.

    Re-fires the same exploratory-rip cycle the user is already in, but
    with the picked title as the new exploratory target. The post-rip
    hook regenerates previews and the user re-enters the ordering UI
    to validate the new set (now including the candidate's extras).
    """
    title_index: int


@router.post("/rip-with-segment-reorder", response_model=JobStatus)
def start_rip_with_segment_reorder(
    req: SegmentReorderStartReq,
    db: Session = Depends(get_db),
):
    """Create a Path A job and start ripping one exploratory playlist.

    Steps:
      1. Resolve disc + cached scan info (titles, segment_map).
      2. Identify the duplicate group containing the user's exploratory
         title index — needed later for build_rip_title_set.
      3. Initialize job.segment_reorder_state = {stage: "exploratory_ripping",
         exploratory_title_index, group_member_indexes, ...}.
      4. Set job.rip_set = [exploratory_title_index] so the worker's
         per-title loop kicks off Phase 1's selective-rip path with just
         the one title.
      5. Dispatch rip_disc.
    """
    from core.disc_manager import get_cached_discs
    from core.path_a_trigger import _auto_pick_exploratory
    from core.segment_reorder import detect_duplicate_segment_groups

    if not req.mount_point:
        raise HTTPException(status_code=400, detail="mount_point is required")

    rip_request_id = uuid.uuid4().hex
    cached = get_cached_discs() or []
    disc_info = next(
        (d for d in cached if d.get("mount_point") == req.mount_point),
        None,
    )
    if not disc_info:
        raise HTTPException(
            status_code=404,
            detail=f"No disc info cached for mount_point={req.mount_point}",
        )

    # #562 PR 5: cache-precondition gate. ``disc_info`` exists but a
    # malformed entry without ``disc_hash`` would still get past the 404
    # above and trip the rip task's cache-miss path. Defer if so.
    from core.disc_scan_dispatch import (
        disc_info_cache_satisfies,
        enqueue_discinfo_scan,
    )
    _disc_num_for_gate = disc_info.get("disc_num")
    if not disc_info_cache_satisfies(req.mount_point, _disc_num_for_gate, None):
        task_id = enqueue_discinfo_scan(
            str(_disc_num_for_gate or "9999"), req.mount_point
        )
        log.info(
            "POST /jobs/rip-with-segment-reorder rid=%s deferring: "
            "disc_scan_in_progress mount_point=%s task_id=%s",
            rip_request_id, req.mount_point, task_id,
        )
        raise HTTPException(
            status_code=409,
            detail={
                "code": "disc_scan_in_progress",
                "mount_point": req.mount_point,
                "discinfo_scan_task_id": task_id,
            },
        )

    # #578: USB-bus-saturation gate. The selective-rip path shares the
    # same physical USB constraint as the standard rip — running two
    # MakeMKV processes on the same sub-SuperSpeed bus saturates it.
    try:
        from core.usb_bus_saturation_policy import evaluate_bus_saturation

        sat_decision = evaluate_bus_saturation(
            req.mount_point,
            db,
            force_override=bool(getattr(req, "force_concurrent_on_saturated_bus", False)),
        )
        if not sat_decision.allowed:
            log.warning(
                "POST /jobs/rip-with-segment-reorder rid=%s blocked by USB "
                "bus saturation: bus=%s speed=%sMbps competing=%s",
                rip_request_id, sat_decision.bus, sat_decision.speed_mbps,
                list(sat_decision.competing_mount_points),
            )
            raise HTTPException(
                status_code=409,
                detail=sat_decision.to_409_payload(),
            )
    except HTTPException:
        raise
    except Exception as sat_exc:
        log.warning(
            "POST /jobs/rip-with-segment-reorder rid=%s USB saturation gate "
            "failed (fail-open): %s",
            rip_request_id, sat_exc,
        )

    titles = disc_info.get("titles") or {}

    groups = detect_duplicate_segment_groups(titles)
    if not groups:
        raise HTTPException(
            status_code=400,
            detail=(
                "No duplicate-segment-map groups detected on this disc; "
                "Path A is not applicable. Use POST /jobs/rip instead."
            ),
        )

    # Auto-pick when caller omits exploratory_title_index — it's a backend
    # concern within a duplicate group (all members share the same segments).
    exploratory_idx = req.exploratory_title_index
    if exploratory_idx is None:
        exploratory_idx = _auto_pick_exploratory(titles, groups)
        if exploratory_idx is None:
            raise HTTPException(
                status_code=500,
                detail="Failed to auto-pick exploratory title from duplicate groups.",
            )
        log.info(
            "POST /jobs/rip-with-segment-reorder rid=%s auto-picked exploratory=%s",
            rip_request_id, exploratory_idx,
        )

    if exploratory_idx not in titles and str(exploratory_idx) not in titles:
        raise HTTPException(
            status_code=400,
            detail=(
                f"exploratory_title_index={exploratory_idx} not in "
                f"disc titles ({sorted(titles.keys())[:8]}...)"
            ),
        )

    matching_group = next(
        (g for g in groups if exploratory_idx in g.title_indexes),
        None,
    )
    if matching_group is None:
        raise HTTPException(
            status_code=400,
            detail=(
                f"exploratory_title_index={exploratory_idx} is "
                f"not a member of any duplicate-segment-map group; Path A "
                f"is not applicable."
            ),
        )

    # Resolve disc_hash + disc_num from cache or request, mirroring start_rip().
    disc_hash = disc_info.get("disc_hash") or disc_info.get("content_hash")
    disc_num = req.disc_num or str(disc_info.get("disc_num") or "")
    if not disc_num:
        raise HTTPException(status_code=400, detail="disc_num is required or could not be determined")

    # NOTE (#560): inline _cleanup_stale_jobs removed from this rip-start
    # request path for the same reason as the plain /jobs/rip endpoint above.
    # The periodic background task handles it.
    log.info(
        "POST /jobs/rip-with-segment-reorder rid=%s exploratory=%s group_size=%s",
        rip_request_id, req.exploratory_title_index, matching_group.size,
    )

    # Use the same gatekeeper.start_rip() that /jobs/rip uses; it creates the
    # job, optimistically advances rip_state to running, and dispatches the
    # rip_disc task. We don't need to bypass it — the threshold-modal frontend
    # has already established this is the "Find canonical" choice, so default
    # rip semantics are fine. We just augment the resulting job with
    # segment_reorder_state + rip_set BEFORE the worker picks it up.
    gatekeeper = DriveGatekeeper(db)
    job = gatekeeper.start_rip(
        disc_hash=disc_hash or "",
        disc_num=disc_num,
        mount_point=req.mount_point,
        mode="copy",
        output_dir=req.output_dir,
        payload=None,
        rip_request_id=rip_request_id,
    )
    if job is None:
        raise HTTPException(status_code=500, detail="Failed to create rip job")

    # Wire selective-rip context onto the job. Note: there's a small race
    # window between gatekeeper.start_rip() dispatching rip_disc and us
    # writing rip_set here. The worker reads job.rip_set inside its session,
    # AFTER the rip slot acquires the FileLock — by then, this commit will
    # have landed. If the race ever materializes (rip_disc reads before
    # this commit), the worst case is one accidental all-mode rip; the
    # next worker tick would see the now-set rip_set on retry/recovery.
    job.segment_reorder_state = {
        "stage": "exploratory_ripping",
        "exploratory_title_index": exploratory_idx,
        "group_member_indexes": list(matching_group.title_indexes),
        "sorted_segment_key": matching_group.sorted_segment_key,
        "submitted_order": None,
        "matched_playlist_index": None,
    }
    job.rip_set = [exploratory_idx]
    # Park the job at the exploratory_rip workflow step so the frontend
    # breadcrumb renders the new pill and the path-a-workspace mounts via
    # currentStep === 'exploratory_rip' (instead of the old stage-based gate).
    # Advanced to the next step (boxset/postprocess) when canonical_complete.
    job.workflow_step = "exploratory_rip"
    db.commit()
    db.refresh(job)

    # Emit workflow-context change so the drive carousel picks up the new
    # job — without this the frontend modal closes and the rip happens
    # silently. Mirrors what /jobs/rip does in start_rip().
    job_id_str = str(job.id)
    disc_id_for_context = (
        req.disc_id if req.disc_id and not req.disc_id.startswith("pending-")
        else (str(job.disc_id) if job.disc_id else None)
    )
    _emit_workflow_context_after_job_creation(job_id_str, disc_id_for_context, db)

    return _build_job_status(job, job_created=True)


@router.post("/{job_id}/segment-order")
def submit_segment_order(
    job_id: str,
    req: SegmentOrderSubmitReq,
    db: Session = Depends(get_db),
):
    """Match the user-supplied segment ordering against on-disc playlists.

    Returns one of three shapes in the response body:
      - `{matched: true, title_index: N}` — exactly one match; caller
        advances the job to canonical-ripping stage.
      - `{matched: false, candidates: [N1, N2, ...]}` — multiple exact
        matches (rare; multi-cut disc with shared runtime); UI surfaces
        a comparison.
      - `{matched: false, candidates: []}` — no exact match; UI prompts
        the user to re-order.
    """
    from core.disc_manager import get_cached_discs
    from core.segment_reorder import match_user_order_to_playlists

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    state = dict(job.segment_reorder_state or {})
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Job is not running a segment-reorder workflow",
        )

    # Locate disc titles via the job's mount_point so we can match the
    # user-submitted ordering against `segment_map`. Cache is the fast
    # path; DB fallback when the cache has expired (typical after a
    # long exploratory rip).
    cached = get_cached_discs() or []
    disc_info = next(
        (d for d in cached if d.get("mount_point") == job.mount_point),
        None,
    )
    titles: dict = {}
    if disc_info:
        titles = disc_info.get("titles") or {}
    if not titles:
        # DB fallback: build a minimal titles dict from disc_titles.
        # match_user_order_to_playlists only reads `segment_map`, so we
        # don't need to populate every cache field.
        disc_id = getattr(job, "disc_id", None)
        if disc_id:
            db_titles = (
                db.query(db_models.DiscTitle)
                .filter(db_models.DiscTitle.disc_id == disc_id)
                .all()
            )
            for t in db_titles:
                if t.index is None:
                    continue
                titles[t.index] = {
                    "segment_map": t.segment_map,
                    "source_file": t.source_file,
                }
            log.info(
                "submit_segment_order: built %d-title map from DB (cache miss) for job %s",
                len(titles), job_id,
            )
    if not titles:
        raise HTTPException(
            status_code=404,
            detail=f"No titles found for mount_point={job.mount_point} (cache miss + empty DB)",
        )

    state["stage"] = "matching_playlists"
    state["submitted_order"] = list(req.order)
    job.segment_reorder_state = state
    db.commit()

    disc_flags = _get_disc_segment_flags(db, getattr(job, "disc_id", None))
    result = match_user_order_to_playlists(titles, req.order, disc_flags=disc_flags)
    if result.has_unique_exact:
        canonical = result.exact[0]
        # Build the final rip_set from Phase 1's helper: all titles minus
        # (group members minus the canonical).
        from core.rip_selection import build_rip_title_set
        from core.segment_reorder import _segment_set_key
        # Re-derive the dedupe group at match time from the current titles
        # dict so the canonical's group is always paren-aware. The stored
        # group_member_indexes in state may have been computed before the
        # paren-normalized _segment_set_key landed (e.g. resuming an older
        # job after deploy); union with the canonical's freshly-computed
        # group to cover both cases.
        canonical_title = titles.get(canonical) if isinstance(titles, dict) else None
        if canonical_title is None and isinstance(canonical, int):
            canonical_title = titles.get(str(canonical))
        canonical_key = (
            _segment_set_key((canonical_title or {}).get("segment_map"))
            if isinstance(canonical_title, dict) else None
        )
        live_group: set[int] = {canonical}
        if canonical_key:
            for tidx, t in titles.items():
                if not isinstance(t, dict):
                    continue
                if _segment_set_key(t.get("segment_map")) == canonical_key:
                    try:
                        live_group.add(int(tidx))
                    except (TypeError, ValueError):
                        continue
        stored_group = set(int(i) for i in (state.get("group_member_indexes") or []))
        merged_group = sorted(live_group | stored_group | {canonical})
        new_rip_set = build_rip_title_set(
            list(titles.keys()),
            canonical_title_index=canonical,
            duplicate_group_member_indexes=merged_group,
        )

        # Exploratory cleanup. The exploratory rip's mkv lives in raw/ and is
        # tracked in ripped_files. Two cases:
        #   1. Exploratory == canonical: the file we already have IS the
        #      canonical. Keep it on disk, keep its ripped_files entry,
        #      and exclude its index from rip_set so the worker doesn't
        #      re-rip it.
        #   2. Exploratory != canonical: the file is junk. Delete it and
        #      remove its ripped_files entry before dispatching the new rip.
        from pathlib import Path as _Path
        from core.job_paths import JobPaths
        exploratory_idx = state.get("exploratory_title_index")
        ripped_files = dict(getattr(job, "ripped_files", None) or {})
        paths = JobPaths.from_job(job)
        raw_dir = _Path(paths.raw)
        exploratory_is_canonical = exploratory_idx is not None and exploratory_idx == canonical
        if exploratory_idx is not None:
            tid_to_drop = next(
                (tid for tid, rel in ripped_files.items()
                 if rel == f"Midway_t{exploratory_idx:02d}.mkv"
                 or rel.endswith(f"_t{exploratory_idx}.mkv")
                 or rel.endswith(f"_t{exploratory_idx:02d}.mkv")),
                None,
            )
            if exploratory_is_canonical:
                log.info(
                    "submit_segment_order: exploratory title %s == canonical; "
                    "keeping rip and excluding from new rip_set",
                    exploratory_idx,
                )
                new_rip_set = [i for i in new_rip_set if i != exploratory_idx]
                # Keep ripped_files entry as-is.
            else:
                # Delete the exploratory mkv from raw/ and from ripped_files.
                if tid_to_drop is not None:
                    rel = ripped_files.pop(tid_to_drop, None)
                    if rel:
                        target = raw_dir / rel
                        try:
                            if target.is_file():
                                target.unlink()
                                log.info(
                                    "submit_segment_order: removed exploratory %s "
                                    "(title %s); canonical=%s",
                                    target, exploratory_idx, canonical,
                                )
                        except OSError as exc:
                            log.warning(
                                "submit_segment_order: failed to remove %s: %s",
                                target, exc,
                            )
                else:
                    log.info(
                        "submit_segment_order: no ripped_files entry found for "
                        "exploratory title %s; nothing to clean up",
                        exploratory_idx,
                    )

        state["matched_playlist_index"] = canonical
        state["stage"] = "canonical_ripping_pending"
        job.segment_reorder_state = state
        job.rip_set = new_rip_set
        job.ripped_files = ripped_files

        # Reset rip_state so the worker can start the canonical rip. The
        # exploratory rip already advanced rip_state to completed; we need
        # to flip it back to running and dispatch a fresh rip_disc task.
        from core.job_state import StageState
        job.rip_state = "running"
        job.rip_progress = 0
        job.rip_phase = "copy"
        # Keep job_status as 'running' (it never advanced past that for
        # Path A jobs) so existing rip-progress callbacks continue to land.
        db.commit()
        db.refresh(job)

        # If everything we needed was already ripped during the exploratory
        # phase (e.g. exploratory == canonical AND no extras), short-circuit
        # the canonical rip and advance directly to the post-rip flow.
        if not new_rip_set:
            from core.job_state import _infer_profile
            branch = _infer_profile(job)
            if branch not in ("hit", "miss"):
                branch = "hit"
            StageState.rip_complete(
                db, job,
                branch=branch,
                ripped_files=ripped_files,
                source_hashes=(getattr(job, "disc_payload", None) or {}).get("source_hashes"),
                reason="submit_segment_order (exploratory was canonical, nothing to re-rip)",
            )
            state["stage"] = "canonical_complete"
            job.segment_reorder_state = state
            # Mirror the canonical_complete advance done by
            # _maybe_advance_canonical_complete: bump workflow_step out of
            # exploratory_rip so the breadcrumb leaves the new pill.
            # #365 Phase 2 § 6.4 — workflow_step="transfer" for hit
            # (postprocess collapsed).
            if getattr(job, "workflow_step", None) == "exploratory_rip":
                job.workflow_step = "transfer" if branch == "hit" else "boxset"
            # Clear the obfuscation_flag MakeMKV set on the matched canonical
            # so the titles UI doesn't render a "Likely decoy" badge on the
            # row Path A just confirmed is the real playlist.
            if _clear_path_a_canonical_obfuscation_flag(job, state, db):
                log.info(
                    "Path A: job %s cleared obfuscation_flag on matched canonical index %s",
                    job_id, state.get("matched_playlist_index"),
                )
            db.commit()
            if branch == "hit":
                # Phase 2 collapse (#365): Path A canonical-completion path
                # uses the unified start_transfer worker too.
                from workers.tasks import start_transfer as start_transfer_task
                start_transfer_task.delay(job_id)
                StageState.postprocess_started(db, job, reason="exploratory was canonical (start_transfer)")
            log.info(
                "submit_segment_order: exploratory was canonical AND no extras to rip; "
                "skipped canonical rip dispatch for job=%s", job_id,
            )
            return {
                "matched": True,
                "title_index": canonical,
                "rip_set_size": 0,
                "sorted_set_match_count": len(result.sorted_set),
                "skipped_canonical_rip": True,
            }

        # Dispatch the canonical rip via the same per-title selective-rip
        # path the exploratory used. The worker reads job.rip_set inside
        # rip_disc and threads it into disc.rip()'s selective-rip path.
        from workers.tasks import rip_disc
        task_result = rip_disc.apply_async(
            args=(str(job.id), str(job.disc_num), job.mount_point, "copy", None),
            kwargs={"rip_request_id": uuid.uuid4().hex},
            task_id=f"rip_disc:canonical:{job.id}",
        )
        job.celery_task_id = task_result.id
        db.commit()
        log.info(
            "submit_segment_order: dispatched canonical rip job=%s rip_set_size=%d task_id=%s",
            job_id, len(new_rip_set), task_result.id,
        )
        return {
            "matched": True,
            "title_index": canonical,
            "rip_set_size": len(new_rip_set),
            "sorted_set_match_count": len(result.sorted_set),
        }

    # No unique exact match: surface candidates for user pick or re-order.
    # Subsequence-superset candidates are returned alongside exact/sorted-set
    # candidates so the frontend can decide what to display (initial render
    # may only show the "didn't match" message; the confirmation-gate flow
    # surfaces supersets after the user re-affirms their order).
    candidates = result.exact or result.sorted_set
    state["stage"] = "awaiting_segment_order"  # back to ordering UI
    job.segment_reorder_state = state
    db.commit()
    return {
        "matched": False,
        "exact_count": len(result.exact),
        "sorted_set_count": len(result.sorted_set),
        "candidates": candidates,
        "subsequence_supersets": [
            _superset_to_dict(c) for c in result.subsequence_supersets
        ],
    }


def _get_disc_segment_flags(db: Session, disc_id: str | None) -> dict[str, str] | None:
    """Pull the per-disc obfuscation-flag dict for the matcher.

    Returns None when no disc_id (job not yet bound to a disc) or when
    flags are empty/missing. The matcher treats None as "no flags".
    """
    if not disc_id:
        return None
    disc = (
        db.query(db_models.Disc)
        .filter(db_models.Disc.id == disc_id)
        .first()
    )
    if not disc:
        return None
    flags = disc.segment_obfuscation_flags or {}
    return dict(flags) if flags else None


def _superset_to_dict(c) -> dict:
    """Serialize a SupersetCandidate for the workflow-context API payload."""
    return {
        "title_index": c.title_index,
        "source_file": c.source_file,
        "extras_clips": list(c.extras_clips),
        "extras_positions": list(c.extras_positions),
        "mpls_total_size_b": c.mpls_total_size_b,
        "sorted_set_key": c.sorted_set_key,
    }


def _load_titles_for_segment_match(job, db) -> dict:
    """Resolve the disc-titles dict for matcher invocation.

    Same cache→DB fallback strategy as `submit_segment_order` —
    pull from the cached disc payload when available, otherwise
    build a minimal `{index: {segment_map, source_file}}` map from
    the disc_titles table.
    """
    from core.disc_manager import get_cached_discs

    cached = get_cached_discs() or []
    disc_info = next(
        (d for d in cached if d.get("mount_point") == job.mount_point),
        None,
    )
    titles: dict = {}
    if disc_info:
        titles = disc_info.get("titles") or {}
    if not titles:
        disc_id = getattr(job, "disc_id", None)
        if disc_id:
            db_titles = (
                db.query(db_models.DiscTitle)
                .filter(db_models.DiscTitle.disc_id == disc_id)
                .all()
            )
            for t in db_titles:
                if t.index is None:
                    continue
                titles[t.index] = {
                    "segment_map": t.segment_map,
                    "source_file": t.source_file,
                }
    return titles


@router.post("/{job_id}/segment-order/confirm")
def confirm_segment_order(
    job_id: str,
    req: SegmentOrderConfirmReq,
    db: Session = Depends(get_db),
):
    """Persist the user's "yes, my order is right" confirmation after a
    no-match result and return matcher candidates including the
    subsequence-superset tier (filtered by the disc's per-clip flags).

    The frontend's confirmation gate calls this after the user re-affirms
    their submitted order. Marks `confirmed_segment_order` so downstream
    iteration steps know the order is user-validated; iteration_history
    gets an `outcome='no_match'` entry so the loop has provenance.
    """
    from core.segment_reorder import match_user_order_to_playlists

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    state = dict(job.segment_reorder_state or {})
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Job is not running a segment-reorder workflow",
        )

    titles = _load_titles_for_segment_match(job, db)
    if not titles:
        raise HTTPException(
            status_code=404,
            detail=f"No titles found for mount_point={job.mount_point}",
        )

    state["confirmed_segment_order"] = list(req.order)
    history = list(state.get("iteration_history") or [])
    history.append({
        "exploratory_title_idx": state.get("exploratory_title_index"),
        "submitted_order": list(req.order),
        "outcome": "no_match",
    })
    state["iteration_history"] = history
    job.segment_reorder_state = state
    db.commit()

    disc_flags = _get_disc_segment_flags(db, getattr(job, "disc_id", None))
    result = match_user_order_to_playlists(titles, req.order, disc_flags=disc_flags)
    return {
        "confirmed": True,
        "exact_count": len(result.exact),
        "sorted_set_count": len(result.sorted_set),
        "subsequence_supersets": [
            _superset_to_dict(c) for c in result.subsequence_supersets
        ],
    }


@router.post("/{job_id}/segment-order/flag-decoys")
def flag_segment_order_decoys(
    job_id: str,
    req: SegmentOrderFlagDecoysReq,
    db: Session = Depends(get_db),
):
    """Mark the exploratory mpls + every sibling sharing its sorted-segment-
    set as `type='ignore'`. Used by the "previous order had decoys" escape
    hatch and at the end of an unproductive iteration.

    Returns the eliminated title indexes so the frontend can update its
    "remaining playlists" counter without a full workflow-context refetch.
    """
    from core.segment_reorder import _segment_set_key

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.disc_id:
        raise HTTPException(
            status_code=400,
            detail="Job has no disc_id; cannot mark decoys",
        )

    state = dict(job.segment_reorder_state or {})
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Job is not running a segment-reorder workflow",
        )

    # Find the exploratory mpls's segment_map to discover its sibling set.
    exploratory = (
        db.query(db_models.DiscTitle)
        .filter(
            db_models.DiscTitle.disc_id == job.disc_id,
            db_models.DiscTitle.index == req.exploratory_title_index,
        )
        .first()
    )
    if not exploratory:
        raise HTTPException(
            status_code=404,
            detail=f"Title index {req.exploratory_title_index} not found on disc",
        )
    target_key = _segment_set_key(exploratory.segment_map)
    if target_key is None:
        # Singleton or unparseable — mark only the exploratory itself.
        siblings = [exploratory]
    else:
        all_titles = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.disc_id == job.disc_id)
            .all()
        )
        siblings = [
            t for t in all_titles
            if _segment_set_key(t.segment_map) == target_key
        ]

    eliminated: list[int] = []
    for t in siblings:
        # Respect user-set types (matches the path A skipped-siblings
        # idempotency pattern in core/path_a_workflow_step).
        if t.type and t.type != "":
            continue
        # flag-decoys is a user-initiated action ("previous order had
        # decoys" button), so user_type owns the ignore. Show-ignored
        # will hide these the same way it hides any other user-ignored
        # row.
        from api.crud import set_title_type
        set_title_type(t, "ignore", source="user")
        if t.index is not None:
            eliminated.append(t.index)

    existing_eliminated = list(state.get("eliminated_title_indexes") or [])
    state["eliminated_title_indexes"] = sorted(set(existing_eliminated + eliminated))
    state["submitted_order"] = None
    state["stage"] = "awaiting_segment_order"
    history = list(state.get("iteration_history") or [])
    history.append({
        "exploratory_title_idx": req.exploratory_title_index,
        "submitted_order": state.get("submitted_order"),
        "outcome": "flagged_decoys",
    })
    state["iteration_history"] = history
    job.segment_reorder_state = state
    db.commit()

    return {
        "eliminated_title_indexes": state["eliminated_title_indexes"],
        "newly_eliminated_count": len(eliminated),
    }


@router.post("/{job_id}/segment-order/rip-superset")
def rip_superset_candidate(
    job_id: str,
    req: SegmentOrderRipSupersetReq,
    db: Session = Depends(get_db),
):
    """Re-fire an exploratory rip on a picked subsequence-superset
    candidate. Reuses the existing job — wipes the prior exploratory's
    previews, sets a fresh exploratory_title_index, and dispatches
    rip_disc with rip_set=[title_index] so the worker selectively rips
    just that one mpls.

    After the rip completes the post-rip hook regenerates previews
    (same path as the initial exploratory) and the user re-enters the
    ordering UI with the new candidate's segments.
    """
    from core.segment_reorder import _segment_set_key
    from workers.tasks import rip_disc

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.disc_id:
        raise HTTPException(
            status_code=400,
            detail="Job has no disc_id; cannot dispatch superset rip",
        )

    state = dict(job.segment_reorder_state or {})
    if not state:
        raise HTTPException(
            status_code=400,
            detail="Job is not running a segment-reorder workflow",
        )

    # Validate the picked title exists on the disc.
    target = (
        db.query(db_models.DiscTitle)
        .filter(
            db_models.DiscTitle.disc_id == job.disc_id,
            db_models.DiscTitle.index == req.title_index,
        )
        .first()
    )
    if not target:
        raise HTTPException(
            status_code=404,
            detail=f"Title index {req.title_index} not found on disc",
        )

    # Identify the sorted-segment-set group the picked title belongs to so
    # the next iteration knows which mpls are siblings (any of them would
    # have produced the same segment sequence — used by flag-decoys if the
    # user later decides this cluster is also wrong).
    target_key = _segment_set_key(target.segment_map)
    if target_key is not None:
        all_titles = (
            db.query(db_models.DiscTitle)
            .filter(db_models.DiscTitle.disc_id == job.disc_id)
            .all()
        )
        siblings = [
            t.index for t in all_titles
            if _segment_set_key(t.segment_map) == target_key and t.index is not None
        ]
    else:
        siblings = [req.title_index]

    # Wipe previews-manifest + submitted_order; record this iteration in history.
    history = list(state.get("iteration_history") or [])
    history.append({
        "exploratory_title_idx": state.get("exploratory_title_index"),
        "submitted_order": state.get("submitted_order"),
        "outcome": "rip_superset",
        "picked_title_index": req.title_index,
    })
    state.update({
        "stage": "exploratory_ripping",
        "exploratory_title_index": req.title_index,
        "group_member_indexes": sorted(set(siblings)),
        "sorted_segment_key": target_key,
        "submitted_order": None,
        "previews_manifest": [],
        "matched_playlist_index": None,
        "iteration_history": history,
    })
    job.segment_reorder_state = state
    job.rip_set = [req.title_index]
    job.rip_state = "running"
    job.rip_progress = 0
    job.rip_phase = "copy"
    job.workflow_step = "exploratory_rip"
    db.commit()

    task_result = rip_disc.apply_async(
        args=(str(job.id), str(job.disc_num), job.mount_point, "copy", None),
        kwargs={"rip_request_id": uuid.uuid4().hex},
        task_id=f"rip_disc:superset:{job.id}:{req.title_index}",
    )
    job.celery_task_id = task_result.id
    db.commit()
    log.info(
        "rip_superset_candidate: dispatched job=%s title=%s task_id=%s",
        job_id, req.title_index, task_result.id,
    )

    return {
        "dispatched": True,
        "exploratory_title_index": req.title_index,
        "rip_set_size": 1,
        "sibling_count": len(siblings),
    }


@router.post("/{job_id}/rip-the-rest")
def rip_the_rest(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Final escape hatch — rip every non-ignored, non-subsumed title on
    the disc and exit the segment-reorder workflow.

    Pre-flight: remaining-playlist-size must fit under the threshold
    (min of 200 GB hard cap and 90% of free disk). If the user has been
    eliminating decoys productively, the remaining storage drops; once
    under the threshold this endpoint is unlocked.

    After dispatch the user lands in the regular title browser with all
    ripped titles visible for manual review — the segment-reorder
    workflow is done.
    """
    import shutil
    from core.segment_reorder import rip_the_rest_threshold_bytes
    from core.utils import get_mkvauto_data
    from workers.tasks import rip_disc

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if not job.disc_id:
        raise HTTPException(
            status_code=400,
            detail="Job has no disc_id; cannot dispatch rip-the-rest",
        )

    titles = (
        db.query(db_models.DiscTitle)
        .filter(db_models.DiscTitle.disc_id == job.disc_id)
        .all()
    )
    rip_indexes: list[int] = []
    remaining_size_b = 0
    for t in titles:
        if t.type == "ignore":
            continue
        if getattr(t, "subsumed_by_title_id", None) is not None:
            continue
        if t.index is None:
            continue
        rip_indexes.append(t.index)
        remaining_size_b += int(t.size or 0)

    if not rip_indexes:
        raise HTTPException(
            status_code=400,
            detail="No rippable titles remain on the disc",
        )

    free_disk_b: int | None = None
    try:
        free_disk_b = shutil.disk_usage(str(get_mkvauto_data())).free
    except Exception:
        pass
    threshold_b = rip_the_rest_threshold_bytes(free_disk_b)
    if remaining_size_b > threshold_b:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "remaining_size_exceeds_threshold",
                "remaining_size_b": remaining_size_b,
                "threshold_b": threshold_b,
                "free_disk_b": free_disk_b,
            },
        )

    state = dict(job.segment_reorder_state or {})
    history = list(state.get("iteration_history") or [])
    history.append({
        "exploratory_title_idx": state.get("exploratory_title_index"),
        "submitted_order": state.get("submitted_order"),
        "outcome": "rip_the_rest",
        "rip_set_size": len(rip_indexes),
        "remaining_size_b": remaining_size_b,
    })
    state.update({
        "stage": "rip_the_rest",
        "iteration_history": history,
    })
    job.segment_reorder_state = state
    job.rip_set = sorted(rip_indexes)
    job.rip_state = "running"
    job.rip_progress = 0
    job.rip_phase = "copy"
    # Leaving the segment-reorder workflow — the user reviews via the
    # regular title browser after the rip lands.
    job.workflow_step = "titles"
    db.commit()

    task_result = rip_disc.apply_async(
        args=(str(job.id), str(job.disc_num), job.mount_point, "copy", None),
        kwargs={"rip_request_id": uuid.uuid4().hex},
        task_id=f"rip_disc:rip_the_rest:{job.id}",
    )
    job.celery_task_id = task_result.id
    db.commit()
    log.info(
        "rip_the_rest: dispatched job=%s rip_set_size=%d remaining=%d task_id=%s",
        job_id, len(rip_indexes), remaining_size_b, task_result.id,
    )

    return {
        "dispatched": True,
        "rip_set_size": len(rip_indexes),
        "remaining_size_b": remaining_size_b,
        "threshold_b": threshold_b,
    }


@router.get("/{job_id}/segment-reorder/preview/{filename:path}")
def stream_segment_preview(
    job_id: str,
    filename: str,
    db: Session = Depends(get_db),
):
    """Stream a per-segment preview .mp4 to the browser.

    Frontend's <video> tag fetches these directly. The path lives under
    `<job.raw_dir>/previews/`. We constrain `filename` to the basename
    of the requested path (no traversal) and require it to be within the
    previews dir.
    """
    from core.job_paths import JobPaths
    from pathlib import Path as _Path

    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    paths = JobPaths.from_job(job)
    previews_dir = (_Path(paths.raw) / "previews").resolve()
    requested = (previews_dir / filename).resolve()

    # Path-traversal guard: requested file must live strictly under previews_dir.
    try:
        requested.relative_to(previews_dir)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid preview path")
    if not requested.is_file():
        raise HTTPException(status_code=404, detail=f"Preview not found: {filename}")

    return FileResponse(
        path=str(requested),
        media_type="video/mp4",
        filename=requested.name,
    )


@router.post("/{job_id}/segment-reorder/cancel", status_code=204)
def cancel_segment_reorder(
    job_id: str,
    db: Session = Depends(get_db),
):
    """Bail out of the Path A workflow. The job is marked failed; the
    exploratory rip artifacts on disk are kept where they are so the
    user can fall back to manual selection without re-ripping.
    """
    job = db.query(db_models.Job).filter(db_models.Job.id == job_id).first()
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")

    state = dict(job.segment_reorder_state or {})
    state["stage"] = "cancelled"
    job.segment_reorder_state = state
    job.job_status = "failed"
    job.error_reason = "segment_reorder_cancelled_by_user"
    # Clear the exploratory_rip pill so any stale carousel render doesn't
    # mount the path-a-workspace on a cancelled job.
    if getattr(job, "workflow_step", None) == "exploratory_rip":
        job.workflow_step = None
    db.commit()
    log.info("Segment-reorder cancelled by user job_id=%s", job_id)
    return Response(status_code=204)
