# Tests for dev.ffmpeg_detection_enabled get/set in core.settings
from __future__ import annotations

from unittest.mock import patch

from core import settings


class TestGetFfmpegDetectionEnabled:
    def test_returns_true_when_set(self):
        with patch.object(settings, "load_settings", return_value={"dev": {"ffmpeg_detection_enabled": True}}):
            assert settings.get_ffmpeg_detection_enabled() is True

    def test_returns_false_when_set(self):
        with patch.object(settings, "load_settings", return_value={"dev": {"ffmpeg_detection_enabled": False}}):
            assert settings.get_ffmpeg_detection_enabled() is False

    def test_returns_true_when_key_missing(self):
        with patch.object(settings, "load_settings", return_value={"dev": {}}):
            assert settings.get_ffmpeg_detection_enabled() is True

    def test_returns_true_when_dev_missing(self):
        with patch.object(settings, "load_settings", return_value={}):
            assert settings.get_ffmpeg_detection_enabled() is True


class TestSetFfmpegDetectionEnabled:
    def test_persists_true(self):
        with patch.object(settings, "load_settings", return_value={"dev": {"quick_postprocess_tests_enabled": True}}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_ffmpeg_detection_enabled(True)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["dev"]["ffmpeg_detection_enabled"] is True
                assert arg["dev"]["quick_postprocess_tests_enabled"] is True

    def test_persists_false(self):
        with patch.object(settings, "load_settings", return_value={"dev": {}}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_ffmpeg_detection_enabled(False)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["dev"]["ffmpeg_detection_enabled"] is False
