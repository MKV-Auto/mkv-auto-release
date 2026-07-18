"""
Backend-owned user notifications: toast (via WebSocket) and optional Discord.

Notifications are emitted by the backend and broadcast on the unified WebSocket.
The frontend subscribes and displays toasts; delivery per channel follows
``notification_preferences`` in settings (see core.notification_preferences).

Dedupe: before sending, we SET notification_dedup:{id} with 24h TTL (NX).
If key already exists we skip sending; otherwise we set and send.

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
from typing import Any, Dict, List, Optional

from core.discord_notification_defaults import DEFAULT_DISCORD_NOTIFICATION_LEVELS
from core.notification_preferences import resolve_delivery_channels

logger = logging.getLogger("core.notifications")

# 24h TTL for dedupe window
NOTIFICATION_DEDUP_TTL_SECONDS = 86400
NOTIFICATION_DEDUP_PREFIX = "notification_dedup:"

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
    "error_transfer",
    "error_generic",
    "action_required",
    "no_transfer_destination",
    "scan_completed",
]

# Default legacy list name (tests/docs); delivery uses notification_preferences.
DEFAULT_DISCORD_LEVELS: List[str] = list(DEFAULT_DISCORD_NOTIFICATION_LEVELS)


def _send_to_discord(message: str, kind: str) -> None:
    """Send a single message to Discord if webhook is configured."""
    try:
        from core import discord_config
        from core.utils import notify_discord
        webhook_url = discord_config.get_webhook_url()
        if not webhook_url:
            return
        emoji = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}.get(kind, "ℹ️")
        formatted = f"{emoji} {message}"
        notify_discord(webhook_url, formatted)
    except Exception as e:
        logger.warning("Failed to send Discord notification: %s", e)


def _notification_id(job_id: Optional[str], level: str, key: Optional[str] = None) -> str:
    """Stable deterministic id for dedupe and push replace. Same logical event => same id."""
    if job_id:
        part = f"{job_id}:{level}"
        return f"{part}:{key}" if key else part
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    return f"sys:{level}:{ts}"


async def _dedupe_should_send(nid: str) -> bool:
    """
    Return True if we should send (first time for this id in 24h), False to skip.
    Uses Redis SET key NX EX 86400; True if SET happened, False if key already existed.
    """
    try:
        import redis.asyncio as aioredis
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/2")
        client = aioredis.Redis.from_url(redis_url, decode_responses=True)
        try:
            dedupe_key = f"{NOTIFICATION_DEDUP_PREFIX}{nid}"
            # SET key 1 NX EX 86400 => True if key was set, False if already existed
            ok = await client.set(dedupe_key, "1", nx=True, ex=NOTIFICATION_DEDUP_TTL_SECONDS)
            return bool(ok)
        finally:
            await client.aclose()
    except Exception as e:
        logger.warning("Notification dedupe check failed, allowing send: %s", e)
        return True


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
        info_title: Optional disc/title name for context (e.g. MakeMKV info_title).
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

    if not await _dedupe_should_send(nid):
        logger.debug(
            "Skipping notification (dedupe window): level=%s id=%s",
            level,
            nid,
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
            _send_to_discord(message, kind)
        except Exception as e:
            logger.warning("Failed to send notification to Discord: %s", e)


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
) -> None:
    """
    Schedule emit_notification from a sync context (e.g. Celery or sync API).
    Uses the FastAPI app event loop if available.
    """
    try:
        from api.main import _app_instance
        app = _app_instance
        if app and hasattr(app, "state") and hasattr(app.state, "event_loop"):
            loop = app.state.event_loop
            if loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    emit_notification(
                        message, kind, level,
                        action_type=action_type,
                        action_payload=action_payload,
                        job_id=job_id,
                        title=title,
                        actions=actions,
                        id_key=id_key,
                        info_title=info_title,
                    ),
                    loop,
                )
                return
    except Exception as e:
        logger.warning("Failed to schedule notification from sync context: %s", e)
    # Fallback: run in new loop (e.g. tests)
    try:
        asyncio.run(emit_notification(
            message, kind, level,
            action_type=action_type,
            action_payload=action_payload,
            job_id=job_id,
            title=title,
            actions=actions,
            id_key=id_key,
            info_title=info_title,
        ))
    except RuntimeError:
        logger.warning("Could not emit notification: no event loop available")
