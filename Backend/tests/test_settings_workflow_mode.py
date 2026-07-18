# Tests for dev.discdb_disabled get/set in core.settings
# (was: dev.workflow_mode_discdb_hit — renamed to make OFF = production
#  behaviour and ON = the test-only deviation, per the devmode-toggle
#  convention.)
from __future__ import annotations

from unittest.mock import patch

from core import settings


class TestGetDiscdbDisabled:
    def test_returns_true_when_set(self):
        with patch.object(
            settings, "load_settings", return_value={"dev": {"discdb_disabled": True}}
        ):
            assert settings.get_discdb_disabled() is True

    def test_returns_false_when_set(self):
        with patch.object(
            settings, "load_settings", return_value={"dev": {"discdb_disabled": False}}
        ):
            assert settings.get_discdb_disabled() is False

    def test_returns_false_when_key_missing(self):
        # Default-OFF matches production behaviour (DiscDB lookups run).
        with patch.object(settings, "load_settings", return_value={"dev": {}}):
            assert settings.get_discdb_disabled() is False

    def test_returns_false_when_dev_missing(self):
        with patch.object(settings, "load_settings", return_value={}):
            assert settings.get_discdb_disabled() is False

    def test_legacy_workflow_mode_discdb_hit_true_maps_to_disabled_false(self):
        # Old key meant "Hit = real lookup"; new equivalent is
        # discdb_disabled=False (don't suppress the lookup).
        with patch.object(
            settings,
            "load_settings",
            return_value={"dev": {"workflow_mode_discdb_hit": True}},
        ):
            assert settings.get_discdb_disabled() is False

    def test_legacy_workflow_mode_discdb_hit_false_maps_to_disabled_true(self):
        # Old key meant "Miss = simulate miss"; new equivalent is
        # discdb_disabled=True.
        with patch.object(
            settings,
            "load_settings",
            return_value={"dev": {"workflow_mode_discdb_hit": False}},
        ):
            assert settings.get_discdb_disabled() is True


class TestSetDiscdbDisabled:
    def test_persists_true(self):
        with patch.object(
            settings,
            "load_settings",
            return_value={"dev": {"quick_postprocess_tests_enabled": True}},
        ):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_discdb_disabled(True)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["dev"]["discdb_disabled"] is True

    def test_persists_false(self):
        with patch.object(settings, "load_settings", return_value={"dev": {}}):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_discdb_disabled(False)
                mock_save.assert_called_once()
                (arg,) = mock_save.call_args[0]
                assert arg["dev"]["discdb_disabled"] is False

    def test_persists_drops_legacy_workflow_mode_key(self):
        # Writing the new setting also strips the old key so they can't
        # drift out of sync.
        with patch.object(
            settings,
            "load_settings",
            return_value={"dev": {"workflow_mode_discdb_hit": True}},
        ):
            with patch.object(settings, "save_settings") as mock_save:
                settings.set_discdb_disabled(True)
                (arg,) = mock_save.call_args[0]
                assert "workflow_mode_discdb_hit" not in arg["dev"]
                assert arg["dev"]["discdb_disabled"] is True
