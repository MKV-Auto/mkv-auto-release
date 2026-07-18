"""Unit tests for core.transfer.utils.notifications.

The historical ``enable_notifications`` per-config toggle was dropped as
redundant with the global Settings -> Notifications preferences. ``should_notify``
now only guards on config presence; per-category / per-channel filtering
lives in the global preferences layer.
"""
import pytest
from types import SimpleNamespace
from unittest.mock import patch

from core.transfer.utils import notifications


def test_should_notify_config_none():
    assert notifications.should_notify(None, "started") is False


def test_should_notify_when_config_present():
    config = SimpleNamespace()
    assert notifications.should_notify(config, "started") is True


def test_notify_transfer_started_skips_when_config_missing():
    with patch.object(notifications, "_send_notification") as send:
        notifications.notify_transfer_started("j1", None)
        send.assert_not_called()


def test_notify_transfer_started_calls_send():
    with patch.object(notifications, "_send_notification") as send:
        config = SimpleNamespace()
        notifications.notify_transfer_started("j1", config, file_name="x.mkv")
        send.assert_called_once()
        msg, kind, level, job_id = send.call_args[0]
        assert "j1" in msg
        assert "x.mkv" in msg
        assert kind == "info"
        assert level == "transfer_started"
        assert job_id == "j1"


def test_notify_transfer_completed_calls_send():
    with patch.object(notifications, "_send_notification") as send:
        config = SimpleNamespace()
        notifications.notify_transfer_completed("j1", config, duration=10.0, speed=50.0)
        send.assert_called_once()
        msg, kind, level, job_id = send.call_args[0]
        assert "completed" in msg.lower() or "j1" in msg
        assert kind == "success"
        assert level == "transfer_completed"
        assert job_id == "j1"


def test_notify_transfer_failed_calls_send():
    with patch.object(notifications, "_send_notification") as send:
        config = SimpleNamespace()
        notifications.notify_transfer_failed("j1", config, error="disk full")
        send.assert_called_once()
        msg, kind, level, job_id = send.call_args[0]
        assert "failed" in msg.lower()
        assert "disk full" in msg
        assert kind == "error"
        assert level == "transfer_failed"
        assert job_id == "j1"
