"""Unit tests for core.discord_config: delegates to core.settings."""
import pytest

from core import discord_config


def test_load_discord_config_returns_patched_dict(monkeypatch):
    stub = {"webhook_url": "https://x", "enabled": True}
    monkeypatch.setattr("core.discord_config.settings.get_discord_dict", lambda: stub)
    assert discord_config.load_discord_config() == stub


def test_save_discord_config_calls_save_discord_dict_with_kwargs(monkeypatch):
    seen = []

    def capture(webhook_url=None, enabled=None, notification_levels=None, notification_preferences=None):
        seen.append(
            {
                "webhook_url": webhook_url,
                "enabled": enabled,
                "notification_levels": notification_levels,
                "notification_preferences": notification_preferences,
            }
        )
        return {}

    monkeypatch.setattr("core.discord_config.settings.save_discord_dict", capture)
    discord_config.save_discord_config(webhook_url="x", enabled=True)
    assert seen == [
        {
            "webhook_url": "x",
            "enabled": True,
            "notification_levels": None,
            "notification_preferences": None,
        }
    ]


def test_get_webhook_url_returns_patched_value(monkeypatch):
    monkeypatch.setattr("core.discord_config.settings.get_webhook_url", lambda: "https://hook")
    assert discord_config.get_webhook_url() == "https://hook"
