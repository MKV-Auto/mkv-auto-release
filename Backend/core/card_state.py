"""One backend-derived state for the job card (#839).

The card carousel used to infer "what is this job doing" client-side from
five inputs (job_status, a spinner set, whichever progress message arrived
last…) and the only words it had were "Unfinished" and "Failed". This module
is the single derivation: JobStatus carries it, and apply_job_state emits a
``job_card_state`` coordinator event on every stage transition, so a card
that isn't the active context still flips the moment the backend does.

Families are the visual contract (mock: issue #839):
- ``your_turn`` — the pipeline is waiting on the user; the pill is a verb.
- ``working``   — the backend has it; the pill names the stage.
- ``done``      — terminal success.
- ``fix``       — terminal failure; the pill is the retry verb.

Deliberately NOT derived here: drive-card states (scanning / ready-to-copy
live with the drive cache, not a job row) and ``stalled`` (a stalled job has
no transitions to emit on; the stale-job watchdog is the right owner and
can emit the same event when it fires — follow-up in #839).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

FAMILY_YOUR_TURN = "your_turn"
FAMILY_WORKING = "working"
FAMILY_DONE = "done"
FAMILY_FIX = "fix"


def _lower(value: Any) -> str:
    return (str(value) if value is not None else "").strip().lower()


def _job_path(job: Any) -> str:
    return f"/activity?jobId={getattr(job, 'id', '')}"


def has_transfer_destination(db: Any) -> Optional[bool]:
    """True/False when determinable; None on lookup failure (treated as True
    so a transient DB hiccup never yells "Pick destination" at the user)."""
    try:
        from core.transfer import service as transfer_service
        return transfer_service.get_active_config(db) is not None
    except Exception as exc:
        logger.debug("has_transfer_destination lookup failed: %s", exc)
        return None


def derive_card_state(job: Any, *, transfer_destination: Optional[bool] = None) -> dict:
    """Map a job row's stage fields to the card contract.

    Returns ``{"card_state", "family", "pill", "progress", "path"}``.
    ``progress`` is the relevant stage's 0–100 snapshot or None. Ordering
    matters: terminal statuses first, then the pipeline walked backwards
    (transfer → post → label → rip) so the furthest-along signal wins.
    """
    job_status = _lower(getattr(job, "job_status", None))
    rip_state = _lower(getattr(job, "rip_state", None))
    label_state = _lower(getattr(job, "label_state", None))
    # post_state is derived, not a column (#365) — Job.derived_post_state.
    post_state = _lower(getattr(job, "derived_post_state", None) or getattr(job, "post_state", None) or "")
    transfer_state = _lower(getattr(job, "transfer_state", None))
    transfer_phase = _lower(getattr(job, "transfer_phase", None))
    rip_phase = _lower(getattr(job, "rip_phase", None))
    profile = _lower(getattr(job, "stage_profile", None) or "miss")
    path = _job_path(job)

    def out(card_state: str, family: str, pill: str, progress: Any = None) -> dict:
        try:
            progress = int(progress) if progress is not None else None
        except (TypeError, ValueError):
            progress = None
        return {"card_state": card_state, "family": family, "pill": pill,
                "progress": progress, "path": path}

    # --- terminal -----------------------------------------------------
    if job_status == "failed":
        # #853: the failure KIND decides whether "Retry" is honest. A
        # precondition failure (inputs missing/unreadable) cannot succeed on
        # retry — the pill names the real remedy; a config failure points at
        # settings. transient/unknown keep the retry verb.
        failure_kind = (getattr(job, "failure_kind", None) or "").strip().lower()
        if failure_kind == "precondition":
            if rip_state == "failed":
                return out("failed_copy", FAMILY_FIX, "See error",
                           getattr(job, "rip_progress", None))
            return out("failed_post" if post_state == "failed" else "failed",
                       FAMILY_FIX, "Re-rip needed",
                       getattr(job, "post_progress", None))
        if failure_kind == "config":
            return out("failed_transfer" if transfer_state == "failed" else "failed",
                       FAMILY_FIX, "Fix settings",
                       getattr(job, "transfer_progress", None))
        if transfer_state == "failed":
            return out("failed_transfer", FAMILY_FIX, "Retry transfer",
                       getattr(job, "transfer_progress", None))
        if post_state == "failed":
            return out("failed_post", FAMILY_FIX, "Retry processing",
                       getattr(job, "post_progress", None))
        if rip_state == "failed":
            return out("failed_copy", FAMILY_FIX, "Retry copy",
                       getattr(job, "rip_progress", None))
        return out("failed", FAMILY_FIX, "See error")
    if job_status == "completed":
        return out("completed", FAMILY_DONE, "In library", 100)

    # --- transfer end of the pipe --------------------------------------
    if transfer_state == "completed":
        # Verification concluded inside the transfer; the remaining click is
        # the user's. This is where the action-required notification belongs.
        return out("ready_to_finish", FAMILY_YOUR_TURN, "Finish", 100)
    if transfer_state == "running":
        if transfer_phase == "verifying" or job_status == "validating":
            return out("verifying", FAMILY_WORKING, "Verifying",
                       getattr(job, "transfer_progress", None))
        return out("transferring", FAMILY_WORKING, "Transferring",
                   getattr(job, "transfer_progress", None))
    if job_status == "validating":
        return out("verifying", FAMILY_WORKING, "Verifying",
                   getattr(job, "transfer_progress", None))
    if transfer_state == "ready":
        if transfer_destination is False:
            return out("needs_destination", FAMILY_YOUR_TURN, "Pick destination")
        return out("ready_to_transfer", FAMILY_YOUR_TURN, "Start transfer")

    # --- postprocess ----------------------------------------------------
    if post_state == "running":
        return out("postprocessing", FAMILY_WORKING, "Post-processing",
                   getattr(job, "post_progress", None))

    # --- label (miss profile only; hit skips labeling) -------------------
    labeling_done = label_state in ("completed", "skipped") or profile == "hit"
    if rip_state == "completed" and not labeling_done:
        return out("awaiting_label", FAMILY_YOUR_TURN, "Label titles")
    if rip_state == "completed" and labeling_done:
        # Between labels-saved and postprocess dispatch (phase=postprocess,
        # post not yet running). The action bar's "Start processing" is the
        # click; auto-started jobs pass through here for milliseconds.
        return out("ready_to_process", FAMILY_YOUR_TURN, "Start processing")

    # --- rip --------------------------------------------------------------
    if rip_state == "running":
        if rip_phase == "verification":
            return out("copying", FAMILY_WORKING, "Checking copy",
                       getattr(job, "rip_progress", None))
        return out("copying", FAMILY_WORKING, "Copying",
                   getattr(job, "rip_progress", None))
    if rip_state in ("pending", "dispatched", "ready"):
        return out("queued", FAMILY_WORKING, "Waiting for drive")

    return out("working", FAMILY_WORKING, "Working")


def build_job_card_state_payload(job: Any, *, db: Any = None) -> dict:
    """The ``job_card_state`` coordinator event body (also embedded in
    JobStatus as card_state/card_family/card_pill)."""
    dest = has_transfer_destination(db) if db is not None else None
    derived = derive_card_state(job, transfer_destination=dest)
    return {
        "job_id": str(getattr(job, "id", "")),
        "disc_id": str(getattr(job, "disc_id", "") or "") or None,
        **derived,
    }


def schedule_job_card_state_event(job: Any, *, db: Any = None) -> None:
    """Fire-and-forget ``job_card_state`` to the coordinator. Never raises."""
    try:
        payload = build_job_card_state_payload(job, db=db)
    except Exception as exc:
        logger.warning("job_card_state payload build failed for %s: %s",
                       getattr(job, "id", None), exc)
        return
    try:
        import asyncio

        async def _emit() -> None:
            from api.routers.websockets import _emit_to_coordinator
            await _emit_to_coordinator("job_card_state", payload)

        loop = None
        try:
            asyncio.get_running_loop()
            asyncio.create_task(_emit())
            return
        except RuntimeError:
            try:
                from api.main import _app_instance
                loop = getattr(getattr(_app_instance, "state", None), "event_loop", None)
            except Exception:
                loop = None
        if loop is not None:
            asyncio.run_coroutine_threadsafe(_emit(), loop)
            return
        # Celery worker process: no uvicorn loop here. Same bridge the
        # progress emitter uses — Redis pub/sub, re-emitted by the API's
        # subscriber (api/main). Without this, worker-side transitions
        # (postprocess/transfer callbacks run in-process in the worker)
        # would never reach the card strip — the exact staleness #839 is
        # about, caught live on the dev rig.
        _publish_via_redis(payload)
    except Exception as exc:
        logger.warning("job_card_state emit failed for %s: %s",
                       getattr(job, "id", None), exc)


COORDINATOR_EVENTS_CHANNEL = "coordinator_events"


def _publish_via_redis(payload: dict) -> None:
    import json
    import os

    import redis

    url = os.getenv("REDIS_URL", "redis://localhost:6379/2")
    if not url.rstrip("/").rsplit("/", 1)[-1].isdigit():
        url = url.rstrip("/") + "/2"
    client = redis.Redis.from_url(url, decode_responses=True)
    client.publish(COORDINATOR_EVENTS_CHANNEL,
                   json.dumps({"type": "job_card_state", **payload}))
