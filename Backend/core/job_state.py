from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Iterable, List, Literal, Mapping, Optional, Tuple

from sqlalchemy.orm import Session

JobStatusValue = Literal["pending", "running", "validating", "completed", "failed"]
StageStateValue = Literal["pending", "ready", "running", "completed", "failed", "skipped"]
PhaseValue = Literal["rip", "label", "finalize", "postprocess", "transfer", "finalize_release", "complete", "failed"]

STATE_FIELDS: tuple[str, ...] = (
    "job_status",
    "scan_state",
    "rip_state",
    "label_state",
    "finalize_state",
    "post_state",
    "transfer_state",
    "finalize_release_state",
    "phase",
)

ALLOWED_JOB_STATUS: set[str] = {"pending", "running", "validating", "completed", "failed"}
ALLOWED_STAGE_STATE: set[str] = {"pending", "ready", "running", "completed", "failed", "skipped"}
ALLOWED_PHASE: set[str] = {"rip", "label", "finalize", "postprocess", "transfer", "finalize_release", "complete", "failed"}

TERMINAL_STAGE_STATES: set[str] = {"completed", "failed", "skipped"}

JOB_STATUS_ORDER: dict[str, int] = {
    "pending": 0,
    "running": 1,
    "validating": 2,
    "completed": 3,
    "failed": 99,
}

_ws_logger = logging.getLogger("core.job_state.websocket")


def _public_app_base_url() -> Optional[str]:
    """Optional absolute base URL for deep links in notifications (Discord, toast actions)."""
    for key in ("MKVAUTO_PUBLIC_URL", "MKVAUTO_FRONTEND_URL", "FRONTEND_URL", "PUBLIC_APP_URL"):
        raw = os.getenv(key)
        if raw and str(raw).strip():
            return str(raw).rstrip("/")
    return None


def _auto_dispatch_will_run(db: Session, job: Any) -> bool:
    """#605: True when the active TransferConfig will trigger an automatic
    transfer immediately after `postprocess_complete` runs.

    The auto-dispatch helpers in `workers/tasks.py`
    (`_maybe_auto_dispatch_local_transfer`, `_maybe_auto_dispatch_remote_transfer`)
    fire microseconds after this notification site, so the
    "post-processing complete. Ready to transfer." toast becomes a
    misleading terminal signal — the user perceives the workflow as
    halted when in fact transfer is starting. Suppress the toast in
    those cases; the user still sees the terminal "Transfer complete"
    notification at the actual end.

    Returns False (keep the toast) when:
    - No active TransferConfig (user hasn't configured a destination).
    - The active config's mode isn't one the auto-dispatch helpers know
      how to run (defensive — future-proofs against new modes).
    - Looking up the config raises any exception (don't silently drop
      a user-visible toast on a transient error).
    """
    try:
        from core.transfer import service as transfer_service
        config = transfer_service.get_active_config(db)
        if not config:
            return False
        mode = getattr(config, "mode", None)
        # local mode auto-dispatches inline via
        # `_maybe_auto_dispatch_local_transfer`; remote modes enqueue
        # `transfer_remote.delay(...)` via the remote helper.
        return mode in ("local", "rsync", "smb", "nfs")
    except Exception as exc:
        _ws_logger.warning(
            "Failed to check auto-dispatch capability for postprocess_complete "
            "notification; keeping notification: %s",
            exc,
        )
        return False


def _labeling_awaiting_copy(job: Any, disc: Any) -> Tuple[str, str]:
    """
    User-facing copy when rip/copy finished and labeling is required.
    Returns (short_title_for_envelope, full_message_body).
    """
    from core.pipeline_notification_labels import (
        job_audience_label,
        job_notification_short_envelope_title,
        job_notification_work_name,
    )

    name = job_notification_work_name(job, disc)
    label = job_audience_label(job, disc)
    msg = f"Copy of {label} completed. Labeling awaiting user completion."
    short = job_notification_short_envelope_title(name)
    return short, msg


def _job_elapsed_suffix_since_created(job: Any) -> str:
    """Suffix like ' (5m 30s)' from job.created_at to now, or ''."""
    created_at = getattr(job, "created_at", None)
    if not created_at:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    if hasattr(created_at, "tzinfo") and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    delta = now - created_at
    total_secs = max(0, int(delta.total_seconds()))
    if total_secs >= 3600:
        h, r = divmod(total_secs, 3600)
        m, s = divmod(r, 60)
        return f" ({h}h {m}m {s}s)"
    if total_secs >= 60:
        m, s = divmod(total_secs, 60)
        return f" ({m}m {s}s)"
    if total_secs > 0:
        return f" ({total_secs}s)"
    return ""


def _emit_transfer_completed_notification(job: Any, job_id: str) -> None:
    """Toast + optional Discord for successful transfer (level transfer_completed)."""
    from core.notifications import emit_notification_sync
    from core.pipeline_notification_labels import job_audience_label

    disc = getattr(job, "disc", None)
    info_title = getattr(disc, "info_title", None) if disc else None
    label = job_audience_label(job, disc)
    elapsed_str = _job_elapsed_suffix_since_created(job)
    # transfer_state=completed implies hash verification already concluded
    # (it happens inside the transfer), so say what the user can DO now —
    # this is the handoff the notification level always claimed to be (#839).
    message = f"Transferred and verified: {label}. Ready to finish.{elapsed_str}"
    emit_notification_sync(
        message,
        "success",
        "transfer_completed",
        job_id=job_id,
        info_title=info_title,
    )


def _emit_job_state_websocket_updates(
    job: Any, normalized_updates: Mapping[str, Any], *, skip_context_changed: bool = False
) -> None:
    """
    Emit websocket updates for job state changes.

    Called after successful database commit in apply_job_state.
    Emits to:
    - Master coordinator (if job becomes unfinished/finished)
    - Per-workflow job websocket (_emit_to_job_workflow, _emit_to_disc_workflow) unless skip_context_changed

    Args:
        job: Job object (already committed to database)
        normalized_updates: Normalized state updates that were applied
        skip_context_changed: If True, do not emit _emit_to_job_workflow nor _emit_to_disc_workflow
            (no context_changed). Used by user-driven POSTs (complete_label, start_postprocess,
            start_transfer, save_label) so the frontend uses the POST response and no fetch/overlay.
    """
    try:
        job_id = str(getattr(job, "id", ""))
        if not job_id:
            return
        
        # Invalidate in-memory workflow context cache for this disc/job
        disc_id = str(getattr(job, "disc_id", "")) if getattr(job, "disc_id", None) else None
        try:
            from api.routers.discs import invalidate_workflow_context_cache
            invalidate_workflow_context_cache(disc_id=disc_id, job_id=job_id)
        except Exception:
            pass  # Best-effort; don't fail the state update if cache invalidation fails
        
        # Check if job status changed to determine if we should emit to master
        job_status = getattr(job, "job_status", None)
        rip_state = getattr(job, "rip_state", None)
        # Superseded = old job replaced by a new rip; don't emit job_finished or context_changed (avoids overwriting UI)
        reason = (getattr(job, "error_reason", None) or "") or ""
        is_superseded = (
            job_status == "failed"
            and ("superseded" in reason.lower() or "starting new rip" in reason.lower())
        )
        effective_skip_context = skip_context_changed or is_superseded
        
        # Determine if job is unfinished (for master websocket)
        is_unfinished = (
            rip_state in ("completed", "skipped") and
            job_status in ("running", "validating")
        )
        was_unfinished = None  # We don't track previous state, so emit if status changed
        
        # Rip started: emit rip_start when we transition to rip running
        if normalized_updates.get("rip_state") == "running":
            try:
                from core.notifications import emit_notification_sync
                from core.pipeline_notification_labels import job_audience_label

                disc = getattr(job, "disc", None)
                info_title = getattr(disc, "info_title", None) if disc else None
                label = job_audience_label(job, disc)
                emit_notification_sync(
                    f"Rip started: {label}",
                    "info",
                    "rip_start",
                    job_id=job_id,
                    info_title=info_title,
                )
            except Exception as exc:
                _ws_logger.warning("Failed to emit rip_start notification: %s", exc)

        # Rip finished successfully (job usually stays running until post/label/transfer complete)
        if normalized_updates.get("rip_state") == "completed" and not is_superseded:
            try:
                from core.notifications import emit_notification_sync

                if getattr(job, "job_status", None) == "failed":
                    pass
                else:
                    from core.pipeline_notification_labels import job_audience_label

                    disc = getattr(job, "disc", None)
                    info_title = getattr(disc, "info_title", None) if disc else None
                    label = job_audience_label(job, disc)
                    # Prefer values from this apply_job_state payload (same atomic transition as
                    # StageState.rip_complete) so milestone text matches verification-complete even if
                    # the ORM refresh/lazy state is momentarily inconsistent.
                    phase = _next_value(job, normalized_updates, "phase")
                    # #365 step 3c — derived. Behaviour preserved across the
                    # transitional + stop-writes modes: rip_complete writes
                    # post_state="ready" (hit) or "pending" (miss); after
                    # stop-writes the helper derives the same values from
                    # rip_state/label_state.
                    post_state = _next_derived_post_state(job, normalized_updates)
                    if phase == "postprocess" and post_state in ("ready", "pending"):
                        emit_notification_sync(
                            f"{label} — rip complete; post-processing will continue automatically.",
                            "success",
                            "rip_complete",
                            job_id=job_id,
                            info_title=info_title,
                        )
                    elif phase == "label" or post_state == "pending":
                        short_title, body = _labeling_awaiting_copy(job, disc)
                        base = _public_app_base_url()
                        actions: Optional[List[Dict[str, Any]]] = None
                        if base:
                            link = f"{base}/ripper"
                            body = f"{body} {link}"
                            actions = [{"label": "Open MKV-Auto", "url": link}]
                        emit_notification_sync(
                            body,
                            "success",
                            "awaiting_labeling",
                            job_id=job_id,
                            title=short_title,
                            info_title=info_title,
                            actions=actions,
                            id_key="labeling",
                        )
                    else:
                        _ws_logger.warning(
                            "Rip milestone: rip_state=completed but unexpected phase=%r post_state=%r "
                            "job_id=%s; emitting generic rip_complete toast",
                            phase,
                            post_state,
                            job_id,
                        )
                        emit_notification_sync(
                            f"Rip complete: {label}.",
                            "success",
                            "rip_complete",
                            job_id=job_id,
                            info_title=info_title,
                        )
            except Exception as exc:
                _ws_logger.warning("Failed to emit rip_complete milestone notification: %s", exc)

        # Post-processing finished; user typically starts transfer next.
        # #365 step 3c — switched discriminator from post_state="completed"
        # to transfer_state="ready" (the unique signature of
        # StageState.postprocess_complete, which writes both atomically
        # alongside phase="transfer"). After stop-writes the post_state
        # write goes away; transfer_state="ready" remains.
        #
        # #605: the standard flow auto-dispatches the actual transfer
        # microseconds after postprocess_complete runs (see
        # `_maybe_auto_dispatch_local_transfer` and
        # `_maybe_auto_dispatch_remote_transfer` in workers/tasks.py).
        # In that case this notification reads as a misleading terminal
        # signal — user gets "post-processing complete. Ready to
        # transfer." then the job-complete notification, two messages
        # for one workflow. Suppress the intermediate notification when
        # the active TransferConfig is auto-dispatch-capable; the
        # user still sees the terminal "Transfer complete" / "Job
        # complete" notification at the actual end. For configs the
        # auto-dispatch helpers won't fire on (no active config / unknown
        # mode), keep the notification so the user knows to click again.
        if (
            normalized_updates.get("transfer_state") == "ready"
            and normalized_updates.get("phase") == "transfer"
        ):
            try:
                if _auto_dispatch_will_run(db, job):
                    pass  # suppressed — auto-dispatch fires immediately after
                else:
                    from core.notifications import emit_notification_sync
                    from core.pipeline_notification_labels import job_audience_label

                    disc = getattr(job, "disc", None)
                    label = job_audience_label(job, disc)
                    emit_notification_sync(
                        f"{label} — post-processing complete. Ready to transfer.",
                        "success",
                        "postprocess_complete",
                        job_id=job_id,
                    )
            except Exception as exc:
                _ws_logger.warning("Failed to emit postprocess_complete notification: %s", exc)

        # Labeling finished (skip_context_changed routes still reach here)
        if normalized_updates.get("label_state") == "completed":
            try:
                from core.notifications import emit_notification_sync
                from core.pipeline_notification_labels import job_audience_label

                disc = getattr(job, "disc", None)
                label = job_audience_label(job, disc)
                emit_notification_sync(
                    f"{label} — labeling complete.",
                    "success",
                    "label_complete",
                    job_id=job_id,
                )
            except Exception as exc:
                _ws_logger.warning("Failed to emit label_complete notification: %s", exc)

        # Transfer stage failed: notify whenever the job is still alive (running OR validating).
        # The job may sit at job_status="validating" between postprocess completion and transfer
        # finalize, so a transfer failure during that window must still produce a toast/Discord
        # notification — without this branch firing, the user sees the failure in the UI but
        # gets no proactive alert. We still skip terminal states (already-failed, completed) and
        # superseded jobs so we don't double-notify on an unrelated cleanup transition.
        if (
            normalized_updates.get("transfer_state") == "failed"
            and getattr(job, "job_status", None) in ("running", "validating")
            and not is_superseded
        ):
            try:
                from core.notifications import emit_notification_sync
                from core.pipeline_notification_labels import job_audience_label

                disc = getattr(job, "disc", None)
                info_title = None
                if disc:
                    rel = getattr(disc, "release", None)
                    movie = getattr(rel, "movie", None) if rel else None
                    info_title = (movie.name if movie else None) or getattr(
                        disc, "info_title", None
                    )
                label = job_audience_label(job, disc)
                err = getattr(job, "transfer_error", None) or ""
                msg = (
                    f"Transfer failed ({label}): {err}" if err else f"Transfer failed ({label})"
                )
                emit_notification_sync(
                    msg,
                    "error",
                    "transfer_failed",
                    job_id=job_id,
                    info_title=info_title,
                    action_type="retry_transfer",
                    action_payload={"job_id": job_id},
                )
            except Exception as exc:
                _ws_logger.warning("Failed to emit transfer_failed notification: %s", exc)

        # Transfer stage succeeded (job may stay running, e.g. miss before finalize/export)
        if normalized_updates.get("transfer_state") == "completed" and not is_superseded:
            try:
                _emit_transfer_completed_notification(job, job_id)
            except Exception as exc:
                _ws_logger.warning("Failed to emit transfer_completed notification: %s", exc)

        # Emit to master coordinator if job status changed
        if "job_status" in normalized_updates or "rip_state" in normalized_updates:
            try:
                from api.routers.websockets import _emit_to_coordinator, _build_disc_metadata, _emit_disc_updated_with_job
                from core.disc_manager import get_cached_discs
                
                # Build full DiscMetadata for unfinished jobs (includes created_at)
                async def _emit_job_unfinished_with_metadata():
                    if hasattr(job, "disc") and job.disc:
                        metadata = _build_disc_metadata(
                            job.disc,
                            disc_state='unfinished',
                            job_id=job_id,
                            created_at=getattr(job, "created_at", None),  # Include job creation time
                            job_status=getattr(job, "job_status", None),
                        )
                        await _emit_to_coordinator("job_unfinished", metadata.model_dump(mode='json'))
                    else:
                        # Fallback to minimal format if disc is not available
                        await _emit_to_coordinator("job_unfinished", {
                            "job_id": job_id,
                            "disc_id": str(job.disc.id) if hasattr(job, "disc") and job.disc else None,
                            "mount_point": getattr(job, "mount_point", None),
                        })
                
                # Check if disc is still inserted and emit disc_updated if job is becoming active
                async def _emit_disc_update_if_inserted():
                    if hasattr(job, "disc") and job.disc:
                        disc_id = str(job.disc.id)
                        # Check if disc is still in cache (inserted)
                        loop = asyncio.get_running_loop()
                        try:
                            cached_discs = await loop.run_in_executor(None, get_cached_discs)
                            is_inserted = any(d.get("disc_id") == disc_id for d in cached_discs)
                            
                            if is_inserted and job_status in ("pending", "running", "validating"):
                                # Disc is inserted and job is active - emit disc_updated with job_id
                                await _emit_disc_updated_with_job(disc_id, job_id)
                                if not skip_context_changed:
                                    from api.routers.websockets import _emit_to_disc_workflow
                                    await _emit_to_disc_workflow(disc_id, changed_fields=['jobStatus'])
                        except Exception as exc:
                            _ws_logger.warning(f"Failed to check if disc is inserted or emit disc update: {exc}")
                
                def _emit_job_finished_with_notification():
                    # Emit job_finished event to coordinator asynchronously
                    async def _emit_coordinator():
                        await _emit_to_coordinator("job_finished", {
                            "job_id": job_id,
                            "disc_id": str(job.disc.id) if hasattr(job, "disc") and job.disc else None,
                            "job_status": job_status,
                        })
                    
                    # Schedule coordinator emission
                    try:
                        loop = asyncio.get_running_loop()
                        asyncio.create_task(_emit_coordinator())
                    except RuntimeError:
                        # No running loop - use threadsafe
                        try:
                            from api.main import _app_instance
                            if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                                loop = _app_instance.state.event_loop
                                asyncio.run_coroutine_threadsafe(_emit_coordinator(), loop)
                        except Exception as coord_exc:
                            _ws_logger.warning("Failed to emit job_finished to coordinator: %s", coord_exc)
                    
                    # Use sync notification emission (has better fallback for Celery workers)
                    try:
                        from core.notifications import emit_notification_sync
                        from core.pipeline_notification_labels import job_audience_label

                        disc = getattr(job, "disc", None)
                        label = job_audience_label(job, disc)
                        if job_status == "completed":
                            info_title = getattr(disc, "info_title", None) if disc else None
                            elapsed_str = _job_elapsed_suffix_since_created(job)
                            transfer_done = (
                                getattr(job, "transfer_state", None) == "completed"
                                and getattr(job, "phase", None) == "complete"
                            )
                            if transfer_done:
                                # Same apply_job_state already emitted transfer_completed above
                                if normalized_updates.get("transfer_state") != "completed":
                                    _emit_transfer_completed_notification(job, job_id)
                            else:
                                message = f"Job complete: {label}{elapsed_str}"
                                emit_notification_sync(
                                    message,
                                    "success",
                                    "job_completed",
                                    job_id=job_id,
                                    info_title=info_title,
                                )
                        elif job_status == "failed":
                            reason = getattr(job, "error_reason", None) or ""
                            # Don't show error toast when job was superseded by a new rip (success path)
                            if "superseded" in reason.lower() or "starting new rip" in reason.lower():
                                pass
                            # #365 step 3c — derived. Postprocess-failed's
                            # apply has job_status=failed AND transfer_state
                            # not in (failed, completed) AND rip_state in
                            # (completed, skipped); the helper returns
                            # "failed" iff this is a postprocess failure
                            # (rip-failed leaves rip_state=failed which
                            # short-circuits derivation to None; transfer-
                            # failed leaves job_status=running). Works
                            # under both transitional (post_state in updates)
                            # and stop-writes (no post_state write) modes.
                            elif _next_derived_post_state(job, normalized_updates) == "failed":
                                msg = reason or "Post-processing failed"
                                emit_notification_sync(
                                    f"{label}: {msg}",
                                    "error",
                                    "postprocess_failed",
                                    job_id=job_id,
                                )
                            elif normalized_updates.get("error_type") == "registration":
                                emit_notification_sync(
                                    f"{label}: MakeMKV needs a valid registration key — its "
                                    "evaluation period has expired, so Blu-ray/UHD discs can't "
                                    "be opened. Enter your key in Settings → MakeMKV, then retry.",
                                    "error",
                                    "error_registration",
                                    job_id=job_id,
                                    title="MakeMKV registration required",
                                )
                            elif normalized_updates.get("error_type") == "disc_read":
                                emit_notification_sync(
                                    f"{label}: The drive couldn't read the disc. Try reinserting the disc or disconnecting and reconnecting the drive.",
                                    "error",
                                    "error_disc_read",
                                    job_id=job_id,
                                    title="Disc read error",
                                )
                            elif normalized_updates.get("rip_state") == "failed" or getattr(
                                job, "rip_state", None
                            ) == "failed":
                                msg = (
                                    f"{label}: Rip failed: {reason}"
                                    if reason
                                    else f"{label}: Rip failed"
                                )
                                emit_notification_sync(
                                    msg, "error", "rip_failed", job_id=job_id
                                )
                            else:
                                emit_notification_sync(
                                    f"{label}: {reason or 'Job failed'}",
                                    "error",
                                    "error_generic",
                                    job_id=job_id,
                                )
                    except Exception as exc:
                        _ws_logger.warning("Failed to emit job_finished notification: %s", exc)

                # Schedule async task (this function may be called from sync context)
                terminal_notification_done = False
                try:
                    loop = asyncio.get_running_loop()
                    if is_unfinished:
                        asyncio.create_task(_emit_job_unfinished_with_metadata())
                    elif job_status in ("completed", "failed") and not is_superseded:
                        # Call directly - it's now a sync function with internal async handling
                        # Skip for superseded so we don't emit job_finished and overwrite the new job's context
                        _emit_job_finished_with_notification()
                        terminal_notification_done = True

                    # Always check if disc is inserted and emit update if job is active
                    if job_status in ("pending", "running", "validating"):
                        asyncio.create_task(_emit_disc_update_if_inserted())
                except RuntimeError:
                    # No running event loop - try to get app reference (e.g. sync callback or thread pool)
                    try:
                        from api.main import _app_instance
                        if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                            loop = _app_instance.state.event_loop
                            if is_unfinished:
                                asyncio.run_coroutine_threadsafe(
                                    _emit_job_unfinished_with_metadata(),
                                    loop
                                )
                            elif job_status in ("completed", "failed") and not is_superseded:
                                # Call directly - it handles event loop internally
                                _emit_job_finished_with_notification()
                                terminal_notification_done = True

                            # Always check if disc is inserted and emit update if job is active
                            if job_status in ("pending", "running", "validating"):
                                asyncio.run_coroutine_threadsafe(_emit_disc_update_if_inserted(), loop)
                    except Exception as exc:
                        _ws_logger.warning(f"Failed to emit job state to coordinator websocket: {exc}")
                if (
                    job_status in ("completed", "failed")
                    and not is_superseded
                    and not terminal_notification_done
                ):
                    # Thread pool / tests: no running loop and no app event loop — still deliver toast/Discord
                    _emit_job_finished_with_notification()
            except ImportError:
                # Websocket module not available (e.g., during tests)
                pass
            except Exception as exc:
                _ws_logger.warning(f"Error emitting job state to coordinator websocket: {exc}")
        
        # Emit to per-workflow job websocket (context_changed) unless skipped
        if not effective_skip_context:
            try:
                from api.routers.websockets import _emit_to_job_workflow

                async def _emit_workflow_notification():
                    try:
                        await _emit_to_job_workflow(job_id, changed_fields=['jobStatus'])
                    except Exception as exc:
                        _ws_logger.warning(f"Failed to emit workflow context change notification to job websocket {job_id}: {exc}")

                try:
                    loop = asyncio.get_running_loop()
                    asyncio.create_task(_emit_workflow_notification())
                except RuntimeError:
                    try:
                        from api.main import _app_instance
                        if _app_instance and hasattr(_app_instance, "state") and hasattr(_app_instance.state, "event_loop"):
                            loop = _app_instance.state.event_loop
                            asyncio.run_coroutine_threadsafe(_emit_workflow_notification(), loop)
                        else:
                            try:
                                asyncio.run(_emit_workflow_notification())
                            except Exception as exc:
                                _ws_logger.warning(f"Failed to emit workflow context without event loop: {exc}")
                    except Exception as exc:
                        _ws_logger.warning(f"Failed to schedule workflow context emission: {exc}")
            except ImportError:
                # Websocket module not available (e.g., during tests)
                pass
            except Exception as exc:
                _ws_logger.warning(f"Error emitting workflow context to job websocket: {exc}")
            
    except Exception as exc:
        _ws_logger.warning(f"Error in _emit_job_state_websocket_updates: {exc}", exc_info=True)


class StateViolation(Exception):
    pass


def _lower(v: Any) -> Optional[str]:
    if v is None:
        return None
    if isinstance(v, str):
        return v.strip().lower()
    return str(v).strip().lower()


def _validate_job_status_transition(current: Optional[str], new: str, *, allow_recovery: bool = False) -> None:
    import json, time, traceback
    if new not in ALLOWED_JOB_STATUS:
        raise StateViolation(f"Invalid job_status: {new}")
    if current is None:
        return
    cur = _lower(current) or "pending"
    if cur == new:
        return
    # always allow terminal failure
    if new == "failed":
        return
    if cur == "failed":
        # Recovery (resume/retry endpoints) is a FIRST-CLASS transition, not a
        # devmode privilege: the old devmode-only escape hatch was stripped
        # from release builds, which left "Retry processing" 409ing on every
        # failed job in production (caught live on 1.6.10).
        if allow_recovery and new in ("pending", "running"):
            return
        raise StateViolation("Cannot transition job_status from failed")
    if cur == "completed":
        raise StateViolation("Cannot transition job_status from completed")

    # Treat running <-> validating as a reversible sub-state switch.
    if {cur, new} == {"running", "validating"}:
        return

    allowed: set[tuple[str, str]] = {
        ("pending", "running"),
        ("running", "completed"),
        ("validating", "completed"),
    }
    if (cur, new) not in allowed:
        raise StateViolation(f"Invalid job_status transition: {cur} -> {new}")


def _validate_stage_transition(current: Optional[str], new: str, field: str) -> None:
    if new not in ALLOWED_STAGE_STATE:
        raise StateViolation(f"Invalid {field}: {new}")
    if current is None:
        return
    cur = _lower(current)
    if not cur or cur == new:
        return
    if cur in TERMINAL_STAGE_STATES and new != cur:
        # Allow transfer_state failed -> running for retry (frontend Retry Transfer button)
        if field == "transfer_state" and cur == "failed" and new == "running":
            return
        raise StateViolation(f"Backward {field} transition not allowed: {cur} -> {new}")
    if cur in ("pending", "ready") and new in ("pending", "ready"):
        # treat pending<->ready as non-destructive reclassification
        return
    if cur == "running" and new in ("pending", "ready"):
        # Allow transfer_state running -> pending for retry after start failure (e.g. share down).
        if field == "transfer_state" and new == "pending":
            return
        raise StateViolation(f"Backward {field} transition not allowed: {cur} -> {new}")


def _validate_phase(value: str) -> None:
    if value not in ALLOWED_PHASE:
        raise StateViolation(f"Invalid phase: {value}")


def _validate_phase_transition(job: Any, normalized_updates: Mapping[str, Any], new_phase: str) -> None:
    """
    Validate phase transitions based on stage states and profile.
    Phase can only advance if previous stages are completed.
    
    DiscDB Hits: Rip -> Postprocess -> Transfer
    DiscDB Misses: Rip -> Label -> Postprocess -> Transfer
    """
    profile = _infer_profile(job)
    
    def get_stage_state(field: str) -> Optional[str]:
        if field in normalized_updates:
            return _lower(normalized_updates.get(field))
        return _lower(getattr(job, field, None))
    
    rip_state = get_stage_state("rip_state")
    label_state = get_stage_state("label_state")
    # #365 step 3b — derived (still honors explicit post_state writes
    # for the transitional period; see _next_derived_post_state).
    post_state = _next_derived_post_state(job, normalized_updates)
    transfer_state = get_stage_state("transfer_state")
    
    # Phase progression rules:
    # DiscDB Hits (profile="hit"):
    #   - rip -> postprocess: requires rip_state == "completed"
    #   - postprocess -> transfer: requires post_state == "completed" AND rip_state == "completed"
    #
    # DiscDB Misses (profile="miss"):
    #   - rip -> label: requires rip_state == "completed"
    #   - label -> postprocess: requires label_state == "completed" AND rip_state == "completed"
    #   - postprocess -> transfer: requires post_state == "completed" AND rip_state == "completed"
    
    if new_phase == "label":
        # Only valid for miss profile
        if profile != "miss":
            raise StateViolation(f"Cannot transition to 'label' phase: profile is {profile!r} (label phase only for miss profile)")
        if rip_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'label' phase: rip_state is {rip_state!r} (must be completed)")
    
    elif new_phase == "postprocess":
        # Rip must always be completed before postprocess (for both hit and miss)
        if rip_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'postprocess' phase: rip_state is {rip_state!r} (must be completed)")
        
        # For miss profile, also require label to be completed (finalize is skipped)
        if profile == "miss":
            if label_state not in ("completed", "skipped"):
                raise StateViolation(f"Cannot transition to 'postprocess' phase: label_state is {label_state!r} (must be completed for miss profile)")
    
    elif new_phase == "transfer":
        # Rip and postprocess must be completed before transfer (for both hit and miss)
        if rip_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'transfer' phase: rip_state is {rip_state!r} (must be completed)")
        if post_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'transfer' phase: post_state is {post_state!r} (must be completed)")
    
    elif new_phase == "finalize_release":
        # All previous stages must be completed
        if rip_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'finalize_release' phase: rip_state is {rip_state!r} (must be completed)")
        if post_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'finalize_release' phase: post_state is {post_state!r} (must be completed)")
        if transfer_state not in ("completed", "skipped"):
            raise StateViolation(f"Cannot transition to 'finalize_release' phase: transfer_state is {transfer_state!r} (must be completed)")


def normalize_state_updates(updates: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Normalize incoming state-related updates:
    - lower-case known state fields
    """
    normalized: Dict[str, Any] = dict(updates)
    for key in STATE_FIELDS:
        if key in normalized:
            normalized[key] = _lower(normalized[key])

    return normalized


def validate_job_state_transition(current: Any, updates: Mapping[str, Any], *, allow_recovery: bool = False) -> None:
    """
    Validate that applying updates to current job state does not create invalid or backward transitions.
    Does not mutate current.

    allow_recovery: permits job_status failed -> pending/running for the
    resume/retry endpoints (a sanctioned recovery, available on release
    builds — never devmode-gated).
    """
    normalized = normalize_state_updates(updates)

    if "job_status" in normalized:
        _validate_job_status_transition(
            getattr(current, "job_status", None), normalized["job_status"],
            allow_recovery=allow_recovery,
        )

    for field in ("scan_state", "rip_state", "label_state", "finalize_state", "post_state", "transfer_state", "finalize_release_state"):
        if field in normalized:
            _validate_stage_transition(getattr(current, field, None), normalized[field], field)

    if "phase" in normalized and normalized["phase"] is not None:
        _validate_phase(normalized["phase"])
        _validate_phase_transition(current, normalized, normalized["phase"])

    # Strong invariant: only allow job_status=completed when required stages are done.
    if normalized.get("job_status") == "completed":
        _validate_completed_invariant(current, normalized)

    # Cross-stage dependencies for in-flight/complete stages.
    _validate_stage_dependencies(current, normalized)


def _infer_profile(job: Any) -> str:
    """
    Infer the job's stage profile: 'hit' or 'miss'.
    Prefers persisted stage_profile; falls back to discdb_result, then disc_payload.label_required.
    """
    profile = _lower(getattr(job, "stage_profile", None))
    if profile in ("hit", "miss"):
        return profile
    
    # Fallback to discdb_result if stage_profile is not set
    discdb_result = _lower(getattr(job, "discdb_result", None))
    if discdb_result in ("hit", "miss"):
        return discdb_result
    
    # Final fallback to disc_payload.label_required
    payload = getattr(job, "disc_payload", None) or {}
    try:
        label_required = bool(payload.get("label_required"))
    except Exception:
        label_required = False
    return "miss" if label_required else "hit"


def _next_value(job: Any, normalized_updates: Mapping[str, Any], field: str) -> Optional[str]:
    if field in normalized_updates:
        return _lower(normalized_updates.get(field))
    return _lower(getattr(job, field, None))


def _next_derived_post_state(job: Any, normalized_updates: Mapping[str, Any]) -> Optional[str]:
    """Compute ``post_state`` as it would be after applying ``normalized_updates``.

    Added in step 3b of the post_state column drop (#365); the
    "transitional" branch that honored an explicit ``post_state``
    value in ``normalized_updates`` was removed in step 5 once the
    column itself was dropped (no caller writes it any more).
    Derives the value from the post-update values of rip_state /
    label_state / transfer_phase / transfer_state / job_status —
    mirrors :meth:`api.models.Job.derived_post_state`.
    """
    rip_state = _next_value(job, normalized_updates, "rip_state")
    if rip_state not in ("completed", "skipped"):
        return None

    job_status = _next_value(job, normalized_updates, "job_status")
    transfer_state = _next_value(job, normalized_updates, "transfer_state")
    if job_status == "failed" and transfer_state not in ("failed", "completed"):
        return "failed"

    transfer_phase = _next_value(job, normalized_updates, "transfer_phase")
    if transfer_phase == "preparing":
        return "running"
    if transfer_phase in ("transferring", "verifying"):
        return "completed"
    if transfer_state == "completed":
        return "completed"
    if transfer_state in ("ready", "running", "failed"):
        return "completed"

    label_state = _next_value(job, normalized_updates, "label_state")
    if label_state in ("completed", "skipped", None):
        return "ready"
    return "pending"


def _validate_completed_invariant(job: Any, normalized_updates: Mapping[str, Any]) -> None:
    """
    Enforce that marking a job completed implies all required pipeline stages are completed/skipped.
    """
    profile = _infer_profile(job)

    def require(field: str, allowed: Iterable[str]) -> None:
        val = _next_value(job, normalized_updates, field)
        if val not in allowed:
            raise StateViolation(f"Cannot complete job: {field} is {val!r} (need one of {sorted(set(allowed))})")

    def require_derived_post_state(allowed: Iterable[str]) -> None:
        # #365 step 3b — derived (still honors explicit post_state writes
        # for the transitional period).
        val = _next_derived_post_state(job, normalized_updates)
        if val not in allowed:
            raise StateViolation(f"Cannot complete job: post_state is {val!r} (need one of {sorted(set(allowed))})")

    # Always require rip/postprocess/transfer to be finished for job completion.
    require("rip_state", ("completed", "skipped"))
    require_derived_post_state(("completed", "skipped"))
    require("transfer_state", ("completed", "skipped"))

    if profile == "miss":
        # Miss profile requires manual labeling + finalize + finalize_release.
        require("label_state", ("completed",))
        require("finalize_state", ("completed",))
        require("finalize_release_state", ("completed",))
    else:
        # Hit profile skips label/finalize/finalize_release.
        require("label_state", ("completed", "skipped"))
        require("finalize_state", ("completed", "skipped"))
        require("finalize_release_state", ("completed", "skipped"))


def _validate_stage_dependencies(job: Any, normalized_updates: Mapping[str, Any]) -> None:
    """
    Enforce stage ordering constraints beyond job completion:
    - transfer requires post+rip done
    - finalize_release requires transfer+post+rip done
    - finalize requires label+rip done
    - label requires scan done and rip at least running
    - post requires rip done
    """
    import logging
    logger = logging.getLogger(__name__)
    
    def require(field: str, allowed: Iterable[str]) -> None:
        val = _next_value(job, normalized_updates, field)
        if val is None:
            raise StateViolation(f"Missing required state {field} while validating dependencies")
        if val not in allowed:
            raise StateViolation(f"Invalid dependency: {field} is {val!r} (need one of {sorted(set(allowed))})")

    def is_running_or_done(field: str) -> bool:
        val = _next_value(job, normalized_updates, field)
        return val in ("running", "completed")

    def is_post_running_or_done() -> bool:
        # #365 step 3b — derived (still honors explicit post_state writes).
        return _next_derived_post_state(job, normalized_updates) in ("running", "completed")

    def require_post_done() -> None:
        val = _next_derived_post_state(job, normalized_updates)
        if val not in ("completed", "skipped"):
            raise StateViolation(f"Invalid dependency: post_state is {val!r} (need one of ['completed', 'skipped'])")

    # If post is running/completed, rip must be completed/skipped.
    if is_post_running_or_done():
        require("rip_state", ("completed", "skipped"))

    # If transfer is running/completed, post and rip must be completed/skipped.
    if is_running_or_done("transfer_state"):
        require_post_done()
        require("rip_state", ("completed", "skipped"))

    # If label is running/completed, scan must be completed/skipped and rip must be running/completed/skipped.
    if is_running_or_done("label_state"):
        require("scan_state", ("completed", "skipped"))
        require("rip_state", ("running", "completed", "skipped"))

    # If finalize is running/completed, label must be completed/skipped and rip completed/skipped.
    if is_running_or_done("finalize_state"):
        require("label_state", ("completed", "skipped"))
        require("rip_state", ("completed", "skipped"))

    # If finalize_release is running/completed, transfer/post/rip must be completed/skipped.
    if is_running_or_done("finalize_release_state"):
        require("transfer_state", ("completed", "skipped"))
        require_post_done()
        require("rip_state", ("completed", "skipped"))
    
    # stage_profile and discdb_result are independent: miss workflow + DiscDB hit (prefill) is valid.
    profile = _infer_profile(job)
    discdb_result_raw = getattr(job, "discdb_result", None) or ""
    discdb_result = discdb_result_raw.lower() if discdb_result_raw else None
    if profile == "miss" and discdb_result == "hit":
        logger.debug(
            "Job %s: stage_profile=miss with discdb_result=hit (DiscDB prefill + full labeling)",
            getattr(job, "id", "unknown"),
        )


def apply_job_state(
    db: Session, job: Any, *, updates: Mapping[str, Any], reason: str | None = None, skip_context_changed: bool = False,
    allow_recovery: bool = False,
) -> Any:
    """
    Validate and apply state updates to a job row.
    Non-state fields are applied as-is; state fields are normalized and validated.
    skip_context_changed: If True, do not emit context_changed (e.g. complete_label uses POST response on frontend).
    allow_recovery: permits job_status failed -> pending/running (resume/retry
    endpoints only) — a first-class transition on release builds.
    """
    import logging
    logger = logging.getLogger(__name__)
    
    job_id = getattr(job, "id", "unknown")
    normalized = normalize_state_updates(updates)
    # Log state transitions for debugging
    state_changes = {}
    for field in STATE_FIELDS:
        if field in normalized:
            old_val = getattr(job, field, None)
            new_val = normalized[field]
            if old_val != new_val:
                state_changes[field] = (old_val, new_val)
    
    if state_changes:
        change_str = ", ".join(f"{field}: {old} -> {new}" for field, (old, new) in state_changes.items())
        logger.info(
            "Job %s: State transition %s%s",
            job_id,
            change_str,
            f" (reason: {reason})" if reason else "",
        )
    validate_job_state_transition(job, normalized, allow_recovery=allow_recovery)

    # Apply updates (normalized for known state fields; as-is for the rest)
    for k, v in normalized.items():
        setattr(job, k, v)

    # Ensure JSON payload mutations are persisted when disc_payload is updated.
    if "disc_payload" in normalized:
        try:
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(job, "disc_payload")
        except Exception:
            pass

    # Explicitly update updated_at timestamp to ensure it's refreshed
    import datetime
    job.updated_at = datetime.datetime.now(datetime.timezone.utc)

    if reason:
        try:
            logs = getattr(job, "logs", None) or []
            logs.append(f"[state] {reason}")
            job.logs = logs[-500:]
        except Exception:
            pass

    db.commit()
    try:
        db.refresh(job)
    except Exception:
        pass

    # Emit websocket updates after successful state change
    _emit_job_state_websocket_updates(job, normalized, skip_context_changed=skip_context_changed)

    # One card_state event per stage transition (#839): the card carousel
    # listens on the coordinator, not the workflow-context channels, and
    # renders this payload verbatim instead of inferring from five fields.
    if state_changes:
        try:
            from core.card_state import schedule_job_card_state_event
            schedule_job_card_state_event(job, db=db)
        except Exception as exc:
            logger.warning("Failed to schedule job_card_state for %s: %s", job_id, exc)

    return job


def _validate_stage_transition_devmode(current: Optional[str], new: str, field: str) -> None:
    """
    Validate stage transition for devmode - allows backward transitions from terminal states.
    Still validates allowed values and basic invariants.
    """
    if new not in ALLOWED_STAGE_STATE:
        raise StateViolation(f"Invalid {field}: {new}")
    if current is None:
        return
    cur = _lower(current)
    if not cur or cur == new:
        return
    # In devmode, allow backward transitions from terminal states (completed/failed/skipped -> pending/ready)
    # This enables stage reversals for testing
    if cur in TERMINAL_STAGE_STATES and new in ("pending", "ready"):
        # Allow backward transitions in devmode
        return
    if cur in ("pending", "ready") and new in ("pending", "ready"):
        # treat pending<->ready as non-destructive reclassification
        return
    # In devmode, allow running -> ready/pending for revert operations (restore from backup)
    if cur == "running" and new in ("pending", "ready"):
        # Allow backward transitions in devmode for revert operations
        return


def apply_job_state_devmode(db: Session, job: Any, *, updates: Mapping[str, Any], reason: str | None = None) -> Any:
    """
    Apply state updates to a job row with devmode rules (allows backward transitions).
    Similar to apply_job_state but bypasses validation for terminal -> pending transitions.
    Still validates allowed state values and basic invariants.
    """
    normalized = normalize_state_updates(updates)
    job_id = getattr(job, "id", "unknown")
    state_changes = {}
    for field in STATE_FIELDS:
        if field in normalized:
            old_val = getattr(job, field, None)
            new_val = normalized[field]
            if old_val != new_val:
                state_changes[field] = (old_val, new_val)
    if state_changes:
        change_str = ", ".join(f"{field}: {old} -> {new}" for field, (old, new) in state_changes.items())
        logger = logging.getLogger(__name__)
        logger.info(
            "Job %s: State transition (devmode) %s%s",
            job_id,
            change_str,
            f" (reason: {reason})" if reason else "",
        )
    # Validate state values (but allow backward transitions)
    if "job_status" in normalized:
        new_status = normalized["job_status"]
        if new_status not in ALLOWED_JOB_STATUS:
            raise StateViolation(f"Invalid job_status: {new_status}")
        # Allow backward transitions in devmode
        current_status = _lower(getattr(job, "job_status", None)) or "pending"
        if current_status == "completed" and new_status in ("pending", "running"):
            # Allow completed -> pending/running in devmode
            pass
        elif current_status == "failed" and new_status != "failed":
            # Allow failed -> pending/running in devmode for recovery/resume operations
            # This enables resume endpoints to recover from failed jobs
            if new_status not in ("pending", "running"):
                raise StateViolation(f"Cannot transition job_status from failed to {new_status} (only pending/running allowed)")
            # Allow the transition
            pass
    
    # Validate stage transitions with devmode rules (allow backward from terminal states)
    for field in ("scan_state", "rip_state", "label_state", "finalize_state", "post_state", "transfer_state", "finalize_release_state"):
        if field in normalized:
            _validate_stage_transition_devmode(getattr(job, field, None), normalized[field], field)
    
    if "phase" in normalized and normalized["phase"] is not None:
        _validate_phase(normalized["phase"])
    
    # Apply updates (normalized for known state fields; as-is for the rest)
    for k, v in normalized.items():
        setattr(job, k, v)

    if reason:
        try:
            logs = getattr(job, "logs", None) or []
            logs.append(f"[state:devmode] {reason}")
            job.logs = logs[-500:]
        except Exception:
            pass

    db.commit()
    try:
        db.refresh(job)
    except Exception:
        pass
    
    # Emit websocket updates after successful state change
    _emit_job_state_websocket_updates(job, normalized)
    
    return job


# ---------------------------------------------------------------------------
# Stage completion: state class (StageState)
# Only these methods may apply stage-related transitions; API/callbacks use them.
# See docs/REFACTOR_RIP_CALLBACK_ARCHITECTURE.md and docs/STATE_MACHINE.md.
# ---------------------------------------------------------------------------

BranchLiteral = Literal["hit", "miss"]


class StageState:
    """
    Canonical stage transition helpers. Only these methods (or the API routes
    that call them) may set rip_state, post_state, phase, label_state, transfer_state
    for stage completion/failure. Workers report outcome via callback; API applies state.
    """

    @staticmethod
    def rip_started(db: Session, job: Any, reason: str | None = None) -> Any:
        """Set job to rip running. Call when API successfully enqueues rip_disc."""
        from datetime import datetime, timezone
        updates: Dict[str, Any] = {
            "rip_state": "running",
            "phase": "rip",
            "job_status": "running",
            "rip_started_at": datetime.now(timezone.utc),
        }
        return apply_job_state(db, job, updates=updates, reason=reason or "rip_started")

    @staticmethod
    def rip_copy_complete(
        db: Session,
        job: Any,
        *,
        reason: str | None = None,
    ) -> Any:
        """After MakeMKV copy is acked: move UX to verification sub-phase; rip still running."""
        updates: Dict[str, Any] = {
            "rip_phase": "verification",
        }
        return apply_job_state(db, job, updates=updates, reason=reason or "rip copy complete (await verify)")

    @staticmethod
    def rip_complete(
        db: Session,
        job: Any,
        *,
        branch: BranchLiteral,
        ripped_files: Dict[str, str],
        source_hashes: Dict[str, str] | None = None,
        reason: str | None = None,
    ) -> Any:
        """Apply rip completion. Caller must enqueue resume_postprocess if branch=='hit'."""
        from datetime import datetime, timezone
        # #365 step 3d — no more post_state write. The value (hit→"ready",
        # miss→"pending") is now derived from rip_state="completed" +
        # label_state="skipped"/"ready" by Job.derived_post_state.
        updates: Dict[str, Any] = {
            "rip_state": "completed",
            "job_status": "running",
            "rip_progress": 100,
            "ripped_files": ripped_files,
            "rip_phase": None,
            "phase": "postprocess" if branch == "hit" else "label",
            "label_state": "skipped" if branch == "hit" else "ready",
            "rip_completed_at": datetime.now(timezone.utc),
        }
        if source_hashes:
            payload = dict(getattr(job, "disc_payload", None) or {})
            payload["source_hashes"] = source_hashes
            updates["disc_payload"] = payload
            # Persist per-title rip hash to DiscTitle.source_hash (#295)
            try:
                from api import models as db_models
                for title_id, hash_value in source_hashes.items():
                    if not title_id or not hash_value:
                        continue
                    title_row = db.query(db_models.DiscTitle).filter(db_models.DiscTitle.id == title_id).first()
                    if title_row:
                        title_row.source_hash = hash_value
            except Exception:
                pass  # Do not fail rip_complete if DiscTitle update fails
        return apply_job_state(db, job, updates=updates, reason=reason or "rip_complete callback")

    @staticmethod
    def rip_failed(
        db: Session,
        job: Any,
        *,
        error_reason: str,
        reason: str | None = None,
        error_type: Optional[str] = None,
        failure_kind: str | None = None,
    ) -> Any:
        """Set rip and job to failed. error_type (e.g. "disc_read") is used for notification only."""
        updates: Dict[str, Any] = {
            "rip_state": "failed",
            "job_status": "failed",
            "failure_kind": failure_kind,
            "error_reason": error_reason,
            "phase": "failed",
            "rip_phase": None,
        }
        if error_type:
            updates["error_type"] = error_type
        return apply_job_state(db, job, updates=updates, reason=reason or "rip_failed callback")

    @staticmethod
    def postprocess_started(
        db: Session,
        job: Any,
        *,
        reason: str | None = None,
        workflow_step: Optional[str] = None,
        error_reason: Optional[str] = None,
    ) -> Any:
        """Set postprocess running. Call when API successfully enqueues resume_postprocess."""
        # #365 step 3d — no more post_state="running" write. We set
        # transfer_phase="preparing" instead, which is what
        # Job.derived_post_state reads to return "running" (the collapsed
        # transient/-drop invariant: preparing IS the postprocess running
        # phase). Setting it here (at API enqueue) rather than waiting for
        # the worker to set it preserves the stuck-detection signal that
        # _cleanup_stale_jobs uses — without this, an API-enqueue followed
        # by a dead worker would never be flagged as stuck (transfer_phase
        # would stay NULL until the worker picks up).
        updates: Dict[str, Any] = {
            "transfer_phase": "preparing",
            "phase": "postprocess",
            "job_status": "running",
        }
        if workflow_step is not None:
            updates["workflow_step"] = workflow_step
        if error_reason is not None:
            updates["error_reason"] = error_reason
        return apply_job_state(db, job, updates=updates, reason=reason or "postprocess_started")

    @staticmethod
    def postprocess_complete(
        db: Session,
        job: Any,
        *,
        post_paths: Dict[str, str],
        post_progress: int = 100,
        disc_payload_updates: Dict[str, Any] | None = None,
        reason: str | None = None,
    ) -> Any:
        """Set postprocess completed, phase=transfer."""
        # #365 step 3d — no more post_state="completed" write. Also clears
        # transfer_phase (which postprocess_started set to "preparing") so
        # Job.derived_post_state doesn't keep returning "running" between
        # this call and the worker's next _advance_transfer_phase. The
        # actual transfer worker advances transfer_phase to "transferring"
        # / "verifying" later; in the brief window between, transfer_state
        # ="ready" is enough for derivation to return "completed" (step 5
        # of the decision table).
        updates: Dict[str, Any] = {
            "post_progress": post_progress,
            "transfer_state": "ready",
            "transfer_phase": None,
            "phase": "transfer",
            "post_paths": post_paths,
        }
        # The worker sets job_status="validating" during postprocess output validation.
        # Reset to "running" so it does not leak into transfer state, where startup
        # recovery would otherwise treat it as needing local artifact reconciliation
        # against a transient/ that transfer has already cleaned up (see #366).
        if getattr(job, "job_status", None) == "validating":
            updates["job_status"] = "running"
        if disc_payload_updates:
            payload = dict(getattr(job, "disc_payload", None) or {})
            payload.update(disc_payload_updates)
            updates["disc_payload"] = payload
        return apply_job_state(db, job, updates=updates, reason=reason or "postprocess_complete callback")

    @staticmethod
    def postprocess_failed(
        db: Session,
        job: Any,
        *,
        error_reason: str,
        reason: str | None = None,
        failure_kind: str | None = None,
    ) -> Any:
        """Set postprocess and job to failed."""
        # #365 step 3d — no more post_state="failed" write. Job.derived_post_state
        # returns "failed" when job_status="failed" + transfer_state not in
        # (failed, completed) + rip_state in (completed, skipped) — i.e. the
        # exact preconditions of this helper.
        updates: Dict[str, Any] = {
            "job_status": "failed",
            "error_reason": error_reason,
            "failure_kind": failure_kind,
        }
        return apply_job_state(db, job, updates=updates, reason=reason or "postprocess_failed callback")

    @staticmethod
    def label_complete(
        db: Session,
        job: Any,
        *,
        reason: str | None = None,
        post_state: str = "ready",  # kept for backward-compat; #365 step 3d ignores this arg
        finalize_state: Optional[str] = None,
        job_status: Optional[str] = None,
    ) -> Any:
        """Set label completed, phase=postprocess. Call from POST /label/complete."""
        # #365 step 3d — no more post_state write. Once label_state="completed"
        # and rip_state in (completed, skipped), Job.derived_post_state returns
        # "ready" (decision-table step 6). The `post_state` kwarg is kept for
        # caller backward-compat but is no longer written; callers can drop it
        # in a future cleanup PR.
        updates: Dict[str, Any] = {
            "label_state": "completed",
            "phase": "postprocess",
        }
        if finalize_state is not None:
            updates["finalize_state"] = finalize_state
        if job_status is not None:
            updates["job_status"] = job_status
        return apply_job_state(
            db, job, updates=updates, reason=reason or "label_complete", skip_context_changed=True
        )

    @staticmethod
    def transfer_complete(
        db: Session,
        job: Any,
        *,
        dest_paths: Any = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Set transfer completed. Call from _complete_transfer."""
        updates: Dict[str, Any] = {
            "transfer_state": "completed",
            "phase": "complete",
            "job_status": "completed",
        }
        if dest_paths is not None:
            updates["transfer_paths"] = dest_paths
        updates.update(kwargs)
        return apply_job_state(db, job, updates=updates, reason=reason or "transfer_complete")

    @staticmethod
    def transfer_failed(
        db: Session,
        job: Any,
        *,
        error_reason: str,
        dest_paths: Any = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """Set transfer stage to failed. Does NOT fail the overall job.

        The job stays running so it remains visible in the UI and the user
        can fix the destination and retry transfer without re-ripping.
        """
        updates: Dict[str, Any] = {
            "transfer_state": "failed",
            "transfer_error": error_reason,
        }
        if kwargs.get("failure_kind") is not None:
            updates["failure_kind"] = kwargs.get("failure_kind")
        if dest_paths is not None:
            updates["transfer_paths"] = dest_paths
        updates.update(kwargs)
        return apply_job_state(db, job, updates=updates, reason=reason or "transfer_failed")


def claim_transfer_for_dispatch(db: Any, job_id: str) -> bool:
    """Atomically claim a job's transfer slot. True if THIS caller won it.

    Two independent paths enqueue ``transfer_remote``: the post-process
    auto-dispatch helper in ``workers/tasks.py`` and the
    ``POST /jobs/{id}/transfer`` endpoint. Neither used to claim the job
    before enqueueing — auto-dispatch left ``transfer_state='ready'``, so
    for the whole gap between enqueue and the worker's own
    ``ready -> running`` transition the job still looked startable.

    On 2026-08-06 that gap was 91 seconds and both paths fired: two celery
    tasks ran the same transfer concurrently and two ``smbclient`` processes
    wrote the SAME destination file. Captain America's Blu-Ray was
    dispatched four times, and its ``NT_STATUS_IO_TIMEOUT`` on 2/8 files is
    what that contention looks like from the SMB side.

    The claim is a single conditional UPDATE so it is atomic even against a
    concurrent claimer: only the transaction whose WHERE still matches gets
    ``rowcount == 1``. Callers must not enqueue when this returns False.
    """
    from api import models

    updated = (
        db.query(models.Job)
        .filter(
            models.Job.id == job_id,
            models.Job.transfer_state.in_(("pending", "ready")),
        )
        .update(
            {"transfer_state": "running", "transfer_progress": 0},
            synchronize_session=False,
        )
    )
    db.commit()
    return updated == 1
