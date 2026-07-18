"""
Canonical default Discord notification_levels when none are stored in settings.

Shared by core.notifications (filtering) and core.settings (GET /system/discord/config
and get_discord_dict fallback) so defaults never drift.
"""

from typing import List

# Keep in sync with pipeline levels in core.notifications.NOTIFICATION_LEVELS.
DEFAULT_DISCORD_NOTIFICATION_LEVELS: List[str] = [
    "rip_complete",
    "job_completed",
    "rip_failed",
    "awaiting_labeling",
    "label_complete",
    "postprocess_complete",
    "transfer_completed",
    "transfer_failed",
    "postprocess_failed",
    "error_disk_space",
    "error_disc_read",
    "error_generic",
    "action_required",
    "no_transfer_destination",
    "scan_completed",
]
