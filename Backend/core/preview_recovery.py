"""
Shared logic for preview regeneration: scan disk vs manifests, auto-recovery caps, Celery activity.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Set, Tuple

from sqlalchemy.orm import Session

from core.job_paths import JobPaths

log = logging.getLogger(__name__)

PREVIEWS_AUTO_RECOVERY_MAX_ATTEMPTS = 5


def active_generate_previews_job_ids() -> Set[str]:
    """Job IDs that currently have a generate_previews task active or reserved on a worker."""
    out: Set[str] = set()
    try:
        from workers.tasks import celery_app

        insp = celery_app.control.inspect()
        for bucket in (insp.active() or {}, insp.reserved() or {}):
            for _worker, tasks in bucket.items():
                for t in tasks or []:
                    name = (t.get("name") or "") or ""
                    if "generate_previews" not in name:
                        continue
                    args = t.get("args") or []
                    if not args:
                        continue
                    first = args[0]
                    if isinstance(first, str) and first:
                        out.add(first)
    except Exception as exc:
        log.debug("active_generate_previews_job_ids: inspect failed: %s", exc)
    return out


def build_preview_regeneration_state(
    job: Any,
    db: Session | None,
    *,
    file_paths_override: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], List[str], str]:
    """
    Build tracks_state and list of track keys that still need encoding (no valid manifest on disk).

    Returns:
        (tracks_state, tracks_to_regenerate, overall_status)
    """
    from workers.tasks import (
        _build_title_id_maps,
        _resolve_preview_rel_path,
        _resolve_preview_title_id,
        _safe_track_folder,
    )

    post_paths = getattr(job, "post_paths", None) or {}
    ripped_files = getattr(job, "ripped_files", None) or {}
    if not post_paths and not ripped_files:
        disc_payload = job.disc_payload or {}
        post_paths = disc_payload.get("post_paths") or {}
        ripped_files = disc_payload.get("ripped_files") or {}

    file_paths: Dict[str, Any] = dict(file_paths_override) if file_paths_override is not None else {}
    if not file_paths:
        file_paths = post_paths if post_paths else ripped_files

    disc_payload = job.disc_payload or {}
    preview_maps = _build_title_id_maps(job, disc_payload)
    existing_previews = disc_payload.get("previews") or {}
    existing_tracks = existing_previews.get("tracks") if isinstance(existing_previews, dict) else {}
    if not isinstance(existing_tracks, dict):
        existing_tracks = {}

    paths = JobPaths.from_job(job)
    preview_root = paths.previews
    tracks_to_regenerate: List[str] = []
    tracks_state: Dict[str, Any] = {}

    preview_paths: Dict[str, Any] = {}
    for raw_key, rel_path in file_paths.items():
        title_id = _resolve_preview_title_id(raw_key, rel_path, preview_maps)
        if title_id:
            preview_paths[str(title_id)] = rel_path

    track_keys_to_check = list(existing_tracks.keys()) if existing_tracks else list(preview_paths.keys())

    for track_key in track_keys_to_check:
        track_info = existing_tracks.get(track_key, {})
        if not isinstance(track_info, dict):
            track_info = {}

        manifest_rel = track_info.get("manifest")
        if not manifest_rel:
            safe = _safe_track_folder(track_key)
            manifest_rel = f"previews/{safe}/preview.m3u8"

        safe_folder = _safe_track_folder(track_key)
        manifest_path = preview_root / safe_folder / "preview.m3u8"

        source = (
            track_info.get("source")
            or preview_paths.get(track_key)
            or _resolve_preview_rel_path(track_key, file_paths, preview_maps)
        )

        if manifest_path.exists():
            tracks_state[track_key] = {
                "status": "completed",
                "manifest": manifest_rel,
                "error": None,
                "source": source,
            }
        else:
            tracks_state[track_key] = {
                "status": "queued",
                "manifest": manifest_rel,
                "error": None,
                "source": source,
            }
            tracks_to_regenerate.append(track_key)

    if not tracks_state:
        overall_status = "queued"
    elif all(v.get("status") == "completed" for v in tracks_state.values()):
        overall_status = "completed"
    elif any(v.get("status") == "completed" for v in tracks_state.values()):
        overall_status = "running"
    else:
        overall_status = "queued"

    return tracks_state, tracks_to_regenerate, overall_status


def user_reset_preview_auto_recovery_metadata(previews: Dict[str, Any]) -> None:
    """Clear auto-recovery counters when the user explicitly requests regeneration."""
    previews["auto_recovery_attempts"] = 0
    previews.pop("auto_recovery_last_error", None)
