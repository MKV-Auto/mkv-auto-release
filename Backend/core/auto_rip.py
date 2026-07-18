"""
Auto-rip on insert (#331).

When the `auto_rip_enabled` setting is on, a completed disc scan dispatches
the rip automatically by calling the same `start_rip` route logic the
Start Copy button uses — so MakeMKV validation, stale-job cleanup, the
Path A modal gate, the gatekeeper's duplicate-start protection, and the
disk-space preflight all apply unchanged.

Hit vs miss:
- DiscDB hit: normal hit profile (rip → postprocess → transfer), job lands
  on the `summary` step.
- DiscDB miss: rip-first. The job is created without requiring a linked
  release; after the rip the user labels as usual (`complete_label` already
  blocks postprocess until the disc is linked). The job lands on the `film`
  step because the user hasn't picked a movie yet — start_rip's default of
  `boxset` assumes the interactive flow already completed the film step.
"""
from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def maybe_auto_start_rip(disc_info: dict) -> Optional[str]:
    """Dispatch a rip for a freshly scanned disc when auto-rip is enabled.

    Returns the job id when a rip was started, else None. Never raises —
    this runs best-effort inside the scan-complete notification path.
    """
    from core import settings

    if not settings.get_auto_rip_enabled():
        return None

    mount_point = disc_info.get("mount_point")
    disc_hash = disc_info.get("disc_hash") or disc_info.get("content_hash")
    if not mount_point:
        return None
    if not disc_hash:
        # Unhashed discs get a temporary disc_id only (events.py temp-id
        # path); identity is too weak for unattended dispatch.
        logger.info("auto-rip: skipping unhashed disc at %s", mount_point)
        return None

    from fastapi import HTTPException

    from api import crud, database
    from api.routers.jobs import start_rip
    from api.schemas import JobCreate

    disc_id = disc_info.get("disc_id")
    discdb_hit = bool(disc_info.get("discdb_hit"))
    req = JobCreate(
        mount_point=str(mount_point),
        disc_id=str(disc_id) if disc_id else None,
        disc_num=str(disc_info.get("disc_num") or "") or None,
    )

    db = database.SessionLocal()
    try:
        try:
            status = start_rip(req, db)
        except HTTPException as exc:
            _report_skip(exc, disc_info)
            return None
        except Exception as exc:
            logger.warning("auto-rip: start_rip failed for %s: %s", mount_point, exc)
            return None

        job_id = status.jobId
        if job_id and not discdb_hit:
            # Rip-first miss: the user hasn't done the film step yet.
            job = crud.get_job(db, job_id)
            if job is not None:
                job.workflow_step = "film"
                db.commit()

        _notify_started(disc_info, job_id, discdb_hit)
        logger.info(
            "auto-rip: started job %s for %s (discdb_hit=%s)",
            job_id, mount_point, discdb_hit,
        )
        return job_id
    finally:
        db.close()


def _report_skip(exc, disc_info: dict) -> None:
    """Log (and for actionable cases notify) why auto-rip did not dispatch."""
    detail = getattr(exc, "detail", None)
    status_code = getattr(exc, "status_code", None)
    mount_point = disc_info.get("mount_point")

    # Path A threshold modal: there is no user present to pick a rip mode,
    # so surface a notification instead of silently doing nothing.
    if status_code == 409 and isinstance(detail, dict) and detail.get("code") == "needs_user_choice":
        try:
            from core.notifications import emit_notification_sync

            # Reuses the existing action_required level so delivery follows the
            # user's action-notification preferences (no new registry entry).
            emit_notification_sync(
                "Auto-rip paused: this disc needs a rip-mode choice (obfuscated playlists). "
                "Open the Ripper to continue.",
                "warning",
                "action_required",
                action_type="open_ripper_drive",
                action_payload={"mount_point": str(mount_point)},
                id_key=f"auto-rip-choice:{disc_info.get('disc_hash') or ''}",
                title="Auto-rip needs your input",
            )
        except Exception as notify_exc:
            logger.warning("auto-rip: failed to emit choice notification: %s", notify_exc)
        return

    if status_code == 409:
        # Gatekeeper says a job already exists for this disc — the normal
        # outcome for re-scans; nothing to do.
        logger.info("auto-rip: not dispatching for %s (existing job): %s", mount_point, detail)
        return

    # 400 disk-space preflight already emitted its own error notification
    # inside start_rip; 503 MakeMKV-missing is surfaced by the settings page.
    logger.warning(
        "auto-rip: start_rip rejected for %s (HTTP %s): %s", mount_point, status_code, detail
    )


def _notify_started(disc_info: dict, job_id: Optional[str], discdb_hit: bool) -> None:
    try:
        from core.notifications import emit_notification_sync

        name = (
            disc_info.get("movie_name")
            or disc_info.get("info_title")
            or disc_info.get("disc_name")
            or "disc"
        )
        if discdb_hit:
            body = f"Auto-rip started for {name}."
        else:
            body = (
                f"Auto-rip started for {name}. "
                "After the copy finishes, link the disc to a movie or series to continue."
            )
        # rip_start is the existing informative level for "a rip began".
        emit_notification_sync(
            body,
            "info",
            "rip_start",
            job_id=str(job_id) if job_id else None,
            id_key=f"auto-rip:{disc_info.get('disc_hash') or ''}",
            title="Auto-rip started",
        )
    except Exception as exc:
        logger.warning("auto-rip: failed to emit started notification: %s", exc)
