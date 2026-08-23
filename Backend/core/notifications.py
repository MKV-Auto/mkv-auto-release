"""
Backend-owned user notifications: toast (via WebSocket) and optional Discord.

Notifications are emitted by the backend and broadcast on the unified WebSocket.
The frontend subscribes and displays toasts; delivery per channel follows
``notification_preferences`` in settings (see core.notification_preferences).

Dedupe: before sending, we SET notification_dedup:{id} with 24h TTL (NX).
If key already exists we skip sending; otherwise we set and send.

The id is derived from ``job_id``/``level``/``id_key`` (see ``_notification_id``).
System-scoped notifications — no job, e.g. a drive fault — dedupe on their
``id_key``, so repeated detections of one condition collapse into one alert.
Two escape hatches keep that from muting a condition the user has since fixed:

* ``dedupe_ttl`` shortens the window for a single call (used by startup checks,
  which would otherwise go quiet across a container restart).
* ``clear_notification_dedupe`` drops the window explicitly on recovery, so the
  next genuine occurrence alerts again. ``core.drive_health.clear_drive_health``
  calls it when a drive starts answering.

Pipeline job toasts/Discord for stage and terminal job status are centralized on
``apply_job_state`` (see docs/NOTIFICATIONS.md). Exceptions include disk-space
alerts and ``transfer_started`` (see that doc).

Discord debugging: confirm Discord is enabled and the webhook URL is set; channel
toggles live under notification preferences. Dedupe can suppress repeats within
24h for the same job/level.

Frontend-originated, local-only toasts (e.g. validation, "Boxset created") stay
in the frontend: components call ToastService.show() with no backend/Discord.
"""
import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from core.discord_notification_defaults import DEFAULT_DISCORD_NOTIFICATION_LEVELS
from core.notification_preferences import resolve_delivery_channels

logger = logging.getLogger("core.notifications")

# 24h TTL for dedupe window
NOTIFICATION_DEDUP_TTL_SECONDS = 86400
NOTIFICATION_DEDUP_PREFIX = "notification_dedup:"

# Short window for conditions re-detected on every process start. Long enough to
# swallow a crash-loop's worth of repeats, short enough that a user who restarts
# to apply a fix still hears about it if the fix did not take.
NOTIFICATION_DEDUP_TTL_STARTUP_SECONDS = 900

# All supported notification levels (for Discord/push filtering and docs).
NOTIFICATION_LEVELS: List[str] = [
    "rip_start",
    "rip_complete",
    "job_completed",
    "rip_failed",
    "per_title",
    "label_complete",
    "postprocess_complete",
    "postprocess_failed",
    "transfer_started",
    "transfer_completed",
    "transfer_failed",
    "awaiting_labeling",
    "previews_ready",
    "error_disk_space",
    "error_disc_read",
    "error_drive_unresponsive",
    "error_transfer",
    "error_generic",
    "action_required",
    "no_transfer_destination",
    "scan_completed",
]

# Default legacy list name (tests/docs); delivery uses notification_preferences.
DEFAULT_DISCORD_LEVELS: List[str] = list(DEFAULT_DISCORD_NOTIFICATION_LEVELS)


def _send_to_discord(message: str, kind: str, job_id: Optional[str] = None) -> None:
    """Send a single message to Discord if webhook is configured.

    When the Base URL setting (#841) is set and the notification carries a
    job, a deep link to that job's workflow is appended — the message
    becomes a door, not just an announcement. Unset base URL leaves the
    message exactly as before."""
    try:
        from core import discord_config
        from core.utils import notify_discord
        webhook_url = discord_config.get_webhook_url()
        if not webhook_url:
            return
        emoji = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}.get(kind, "ℹ️")
        formatted = f"{emoji} {message}"
        if job_id:
            try:
                from core.settings import get_base_url
                base = get_base_url()
                if base:
                    formatted += f"\n🔗 Open job: {base}/activity?jobId={job_id}"
            except Exception as link_exc:
                logger.debug("Skipping Discord deep link: %s", link_exc)
        notify_discord(webhook_url, formatted)
    except Exception as e:
        logger.warning("Failed to send Discord notification: %s", e)


def _notification_id(job_id: Optional[str], level: str, key: Optional[str] = None) -> str:
    """Stable deterministic id for dedupe and push replace. Same logical event => same id."""
    if job_id:
        part = f"{job_id}:{level}"
        return f"{part}:{key}" if key else part
    if key:
        # System-scoped but identified: the caller named the condition (a mount
        # point, a bus, a disc hash), so honour it. Timestamping here instead
        # would mint a new id every second and silently defeat the dedupe the
        # caller asked for.
        return f"sys:{level}:{key}"
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"sys:{level}:{ts}"


async def _dedupe_should_send(
    nid: str,
    ttl_seconds: int = NOTIFICATION_DEDUP_TTL_SECONDS,
) -> bool:
    """
    Return True if we should send (first time for this id in the TTL), False to skip.
    Uses Redis SET key NX EX ttl; True if SET happened, False if key already existed.
    """
    try:
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/2")
        client = aioredis.Redis.from_url(redis_url, decode_responses=True)
        try:
            dedupe_key = f"{NOTIFICATION_DEDUP_PREFIX}{nid}"
            # SET key 1 NX EX ttl => True if key was set, False if already existed
            ok = await client.set(dedupe_key, "1", nx=True, ex=ttl_seconds)
            return bool(ok)
        finally:
            await client.aclose()
    except Exception as e:
        logger.warning("Notification dedupe check failed, allowing send: %s", e)
        return True


async def _dedupe_forget(nid: str) -> bool:
    """Drop the dedupe window for *nid*. True when a window was actually open.

    Best-effort like the check itself: Redis being unreachable must not break
    the recovery path that called us.
    """
    try:
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/2")
        client = aioredis.Redis.from_url(redis_url, decode_responses=True)
        try:
            removed = await client.delete(f"{NOTIFICATION_DEDUP_PREFIX}{nid}")
            return bool(removed)
        finally:
            await client.aclose()
    except Exception as e:
        logger.warning("Notification dedupe clear failed for id=%s: %s", nid, e)
        return False


async def clear_notification_dedupe(
    level: str,
    id_key: Optional[str] = None,
    job_id: Optional[str] = None,
) -> bool:
    """Re-arm *level*/*id_key* so the next matching notification sends again.

    Call this when the condition a notification announced has demonstrably
    cleared. Without it a recoverable fault stays muted for the full TTL, even
    after the user fixes it and triggers the same fault again.
    """
    nid = _notification_id(job_id, level, id_key)
    cleared = await _dedupe_forget(nid)
    if cleared:
        logger.info("Cleared notification dedupe window: id=%s", nid)
    return cleared


async def emit_notification(
    message: str,
    kind: str,
    level: str,
    action_type: Optional[str] = None,
    action_payload: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    title: Optional[str] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    id_key: Optional[str] = None,
    info_title: Optional[str] = None,
    dedupe_ttl: Optional[int] = None,
) -> None:
    """
    Emit a user-facing notification: broadcast on unified WebSocket and optionally send to Discord.

    Payload includes a stable envelope (id, timestamp, source) for dedupe and push.

    Args:
        message: Text to show in toast and (if Discord) in channel.
        kind: Toast kind: "info" | "success" | "warning" | "error".
        level: Notification level for Discord filtering (e.g. rip_complete, rip_failed).
        action_type: Optional action for frontend (e.g. open_transfer_setup).
        action_payload: Optional payload for the action.
        job_id: Optional job id (used for stable notification id and payload).
        title: Optional short title (defaults to message for display).
        actions: Optional list of { label, url } for frontend actions.
        id_key: Optional extra key for id when multiple events per job (e.g. per_title).
            Without a job_id this is what makes the id stable, so repeats of one
            condition dedupe instead of alerting per occurrence.
        info_title: Optional disc/title name for context (e.g. MakeMKV info_title).
        dedupe_ttl: Optional override (seconds) for the dedupe window. Defaults to
            NOTIFICATION_DEDUP_TTL_SECONDS.
    """
    nid = _notification_id(job_id, level, id_key)
    now = datetime.now(timezone.utc).isoformat()
    payload: Dict[str, Any] = {
        "type": "notification",
        "id": nid,
        "timestamp": now,
        "source": "backend",
        "message": message,
        "kind": kind,
        "level": level,
    }
    if job_id is not None:
        payload["job_id"] = job_id
    if title is not None:
        payload["title"] = title
    if info_title is not None:
        payload["info_title"] = info_title
    if action_type is not None:
        payload["action_type"] = action_type
    if action_payload is not None:
        payload["action_payload"] = action_payload
    if actions is not None:
        payload["actions"] = actions

    try:
        from core import discord_config
        cfg = discord_config.load_discord_config()
    except Exception as e:
        logger.warning("Failed to load discord config for notification: %s", e)
        cfg = {}

    if not isinstance(cfg, dict):
        cfg = {}
    send_ws, send_discord = resolve_delivery_channels(level, cfg)
    webhook_configured = bool((cfg.get("webhook_url") or "").strip()) and bool(
        cfg.get("enabled", False)
    )
    if not send_ws and not send_discord:
        logger.info(
            "Skipping notification (no delivery channels): level=%s id=%s send_ws=%s "
            "send_discord=%s discord.enabled=%s webhook_configured=%s",
            level,
            nid,
            send_ws,
            send_discord,
            bool(cfg.get("enabled", False)),
            webhook_configured,
        )
        return

    ttl = NOTIFICATION_DEDUP_TTL_SECONDS if dedupe_ttl is None else dedupe_ttl
    if not await _dedupe_should_send(nid, ttl):
        logger.debug(
            "Skipping notification (dedupe window): level=%s id=%s ttl=%s",
            level,
            nid,
            ttl,
        )
        return

    if send_ws:
        try:
            from api.routers.websockets import _emit_unified
            await _emit_unified(payload)
        except Exception as e:
            logger.warning("Failed to emit notification to WebSocket: %s", e)

    if send_discord:
        try:
            _send_to_discord(message, kind, job_id=job_id)
        except Exception as e:
            logger.warning("Failed to send notification to Discord: %s", e)


def _run_from_sync(make_coro: Callable[[], Awaitable[Any]], description: str) -> None:
    """Run *make_coro()* from a sync context (e.g. Celery or a sync API handler).

    Prefers the FastAPI app event loop; falls back to a fresh loop (e.g. tests).
    Takes a factory rather than a coroutine so the fallback path can build a
    second one instead of re-awaiting a consumed object.
    """
    try:
        from api.main import _app_instance
        app = _app_instance
        if app and hasattr(app, "state") and hasattr(app.state, "event_loop"):
            loop = app.state.event_loop
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(make_coro(), loop)
                return
    except Exception as e:
        logger.warning("Failed to schedule %s from sync context: %s", description, e)
    # Fallback: run in new loop (e.g. tests)
    try:
        asyncio.run(make_coro())
    except RuntimeError:
        logger.warning("Could not run %s: no event loop available", description)


def emit_notification_sync(
    message: str,
    kind: str,
    level: str,
    action_type: Optional[str] = None,
    action_payload: Optional[Dict[str, Any]] = None,
    job_id: Optional[str] = None,
    title: Optional[str] = None,
    actions: Optional[List[Dict[str, Any]]] = None,
    id_key: Optional[str] = None,
    info_title: Optional[str] = None,
    dedupe_ttl: Optional[int] = None,
) -> None:
    """
    Schedule emit_notification from a sync context (e.g. Celery or sync API).
    Uses the FastAPI app event loop if available.
    """
    _run_from_sync(
        lambda: emit_notification(
            message, kind, level,
            action_type=action_type,
            action_payload=action_payload,
            job_id=job_id,
            title=title,
            actions=actions,
            id_key=id_key,
            info_title=info_title,
            dedupe_ttl=dedupe_ttl,
        ),
        "notification",
    )


def clear_notification_dedupe_sync(
    level: str,
    id_key: Optional[str] = None,
    job_id: Optional[str] = None,
) -> None:
    """Schedule clear_notification_dedupe from a sync context."""
    _run_from_sync(
        lambda: clear_notification_dedupe(level, id_key=id_key, job_id=job_id),
        "notification dedupe clear",
    )
