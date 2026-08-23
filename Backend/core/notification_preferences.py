"""
Notification delivery preferences: map pipeline levels to informative / action_required / error,
defaults, migration from legacy notification_levels, and per-channel resolution for WebSocket vs Discord.
"""
from __future__ import annotations

import copy
import logging
from typing import Any, Dict, List, Literal, Optional, Set, Tuple

logger = logging.getLogger("core.notification_preferences")

NotificationType = Literal["informative", "action_required", "error"]

# Informative: progress-style events (can be fully silenced via master toggle).
INFORMATIVE_CATEGORIES: Tuple[str, ...] = (
    "rip_start",
    "rip_complete",
    "job_completed",
    "per_title",
    "previews_ready",
    "transfer_started",
    # "Labeling complete" is the user's own action echoed back — a milestone,
    # not a handoff — so it lives here (off by default with the rest) rather
    # than in the action-required bucket it shipped in.
    "label_complete",
)

# User handoffs and milestones that need attention.
ACTION_LEVELS: Set[str] = {
    "awaiting_labeling",
    "postprocess_complete",
    "transfer_completed",
    "action_required",
    "no_transfer_destination",
    "scan_completed",
}

ERROR_LEVELS: Set[str] = {
    "rip_failed",
    "postprocess_failed",
    "transfer_failed",
    "error_disk_space",
    "error_disc_read",
    "error_drive_unresponsive",
    "error_transfer",
    "error_generic",
}


def default_notification_preferences() -> Dict[str, Any]:
    cats = {
        cat: {"in_app": True, "discord": True}
        for cat in INFORMATIVE_CATEGORIES
    }
    return {
        "informative": {
            "enabled": False,
            "categories": cats,
        },
        "action_required": {"in_app": True, "discord": True},
        "errors": {"in_app": True, "discord": True},
    }


def level_bucket(level: str) -> Tuple[NotificationType, str]:
    """Map a pipeline level to (type, category_id). category_id matches informative row keys."""
    if level in INFORMATIVE_CATEGORIES:
        return ("informative", level)
    if level in ACTION_LEVELS:
        return ("action_required", level)
    if level in ERROR_LEVELS:
        return ("error", level)
    logger.debug("Unknown notification level %r; treating as error", level)
    return ("error", level)


def migrate_levels_list_to_preferences(levels: List[str]) -> Dict[str, Any]:
    """Build preferences from legacy discord.notification_levels list."""
    s = set(levels)
    out = copy.deepcopy(default_notification_preferences())
    out["informative"]["enabled"] = bool(s & set(INFORMATIVE_CATEGORIES))
    for cat in INFORMATIVE_CATEGORIES:
        out["informative"]["categories"][cat]["in_app"] = True
        out["informative"]["categories"][cat]["discord"] = cat in s
    out["action_required"]["in_app"] = True
    # Legacy list only mapped action-required Discord when an ACTION_LEVEL was present.
    # Also enable the action-required Discord bucket if any error level was listed, so
    # users who used Discord for failures still receive handoff-style levels (e.g. scan_completed).
    out["action_required"]["discord"] = bool(s & ACTION_LEVELS) or bool(s & ERROR_LEVELS)
    out["errors"]["in_app"] = True
    out["errors"]["discord"] = bool(s & ERROR_LEVELS)
    return out


def normalize_notification_preferences(raw: Any) -> Dict[str, Any]:
    """Merge user dict with defaults; coerce booleans; drop unknown informative category keys."""
    base = copy.deepcopy(default_notification_preferences())
    if not isinstance(raw, dict):
        return base
    inf = raw.get("informative")
    if isinstance(inf, dict):
        if "enabled" in inf:
            base["informative"]["enabled"] = bool(inf["enabled"])
        cats = inf.get("categories")
        if isinstance(cats, dict):
            for cat in INFORMATIVE_CATEGORIES:
                row = cats.get(cat)
                if isinstance(row, dict):
                    if "in_app" in row:
                        base["informative"]["categories"][cat]["in_app"] = bool(row["in_app"])
                    if "discord" in row:
                        base["informative"]["categories"][cat]["discord"] = bool(row["discord"])
    ar = raw.get("action_required")
    if isinstance(ar, dict):
        if "in_app" in ar:
            base["action_required"]["in_app"] = bool(ar["in_app"])
        if "discord" in ar:
            base["action_required"]["discord"] = bool(ar["discord"])
    err = raw.get("errors")
    if isinstance(err, dict):
        if "in_app" in err:
            base["errors"]["in_app"] = bool(err["in_app"])
        if "discord" in err:
            base["errors"]["discord"] = bool(err["discord"])
    return base


def resolve_delivery_channels(level: str, discord_cfg: Dict[str, Any]) -> Tuple[bool, bool]:
    """
    Return (send_websocket, send_discord) for this level and stored preferences.
    discord_cfg: load_discord_config() shape with webhook_url, enabled, notification_preferences.
    """
    prefs = discord_cfg.get("notification_preferences")
    if not isinstance(prefs, dict):
        prefs = default_notification_preferences()
    else:
        prefs = normalize_notification_preferences(prefs)

    ntype, cat_id = level_bucket(level)
    webhook_ok = bool(discord_cfg.get("enabled")) and bool(
        (discord_cfg.get("webhook_url") or "").strip()
    )

    if ntype == "informative":
        if not prefs["informative"]["enabled"]:
            return (False, False)
        row = prefs["informative"]["categories"].get(cat_id)
        if not isinstance(row, dict):
            row = {"in_app": True, "discord": True}
        in_app = bool(row.get("in_app", True))
        want_discord = bool(row.get("discord", True))
        return (in_app, want_discord and webhook_ok)

    if ntype == "action_required":
        ar = prefs["action_required"]
        return (
            bool(ar.get("in_app", True)),
            bool(ar.get("discord", True)) and webhook_ok,
        )

    # error
    er = prefs["errors"]
    return (
        bool(er.get("in_app", True)),
        bool(er.get("discord", True)) and webhook_ok,
    )
