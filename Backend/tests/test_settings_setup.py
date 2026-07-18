# Tests for first_time_setup_complete and setup_step get/set in core.settings
from __future__ import annotations

from unittest.mock import patch

from core import settings


class TestGetFirstTimeSetupComplete:
    def test_returns_true_when_set(self):
        with patch.object(
            settings, "load_settings", return_value={"first_time_setup_complete": True}
        ):
            assert settings.get_first_time_setup_complete() is True

    def test_returns_false_when_set(self):
        with patch.object(
            settings, "load_settings", return_value={"first_time_setup_complete": False}
        ):
            assert settings.get_first_time_setup_complete() is False

    def test_returns_false_when_key_missing(self):
        with patch.object(settings, "load_settings", return_value={}):
            assert settings.get_first_time_setup_complete() is False


class TestSetFirstTimeSetupComplete:
    def test_persists_true(self):
        with patch.object(settings, "load_settings", return_value={"first_time_setup_complete": False}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_first_time_setup_complete(True)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["first_time_setup_complete"] is True

    def test_persists_false(self):
        with patch.object(settings, "load_settings", return_value={"first_time_setup_complete": True}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_first_time_setup_complete(False)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["first_time_setup_complete"] is False


class TestGetSetupStep:
    def test_returns_step_when_valid(self):
        for step in (1, 3, 6):
            with patch.object(settings, "load_settings", return_value={"setup_step": step}):
                assert settings.get_setup_step() == step

    def test_returns_1_when_key_missing(self):
        with patch.object(settings, "load_settings", return_value={}):
            assert settings.get_setup_step() == 1

    def test_returns_1_when_invalid(self):
        with patch.object(settings, "load_settings", return_value={"setup_step": 0}):
            assert settings.get_setup_step() == 1
        with patch.object(settings, "load_settings", return_value={"setup_step": 7}):
            assert settings.get_setup_step() == 1


class TestSetSetupStep:
    def test_persists_valid_step(self):
        with patch.object(settings, "load_settings", return_value={"setup_step": 1}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_setup_step(3)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["setup_step"] == 3

    def test_clamps_to_1_6(self):
        with patch.object(settings, "load_settings", return_value={"setup_step": 1}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_setup_step(0)
                (arg,) = mock_save.call_args[0]
                assert arg["setup_step"] == 1
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_setup_step(10)
                (arg,) = mock_save.call_args[0]
                assert arg["setup_step"] == 6
