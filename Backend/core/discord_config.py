"""
Persisted Discord notification settings.
Delegates to core.settings (settings.json).
"""
from typing import List, Optional

from core import settings


def load_discord_config():
    return settings.get_discord_dict()


def save_discord_config(
    webhook_url: Optional[str] = None,
    enabled: Optional[bool] = None,
    notification_levels: Optional[List[str]] = None,
    notification_preferences: Optional[dict] = None,
):
    return settings.save_discord_dict(
        webhook_url=webhook_url,
        enabled=enabled,
        notification_levels=notification_levels,
        notification_preferences=notification_preferences,
    )


def get_webhook_url() -> Optional[str]:
    return settings.get_webhook_url()
