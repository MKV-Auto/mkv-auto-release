"""Tests for the auto-rip settings flag (#331).

Direct settings round-trip; the API endpoint surfaces (`GET/POST
/system/auto-rip/config`) wrap these getters/setters and are exercised
by integration tests in the verification batch where the full app
readiness gate (DB / Redis warm-up) is satisfied.
"""
import pytest

from core import settings


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path, monkeypatch):
    """Override the settings file location so tests don't pollute user state."""
    test_settings_file = tmp_path / "settings.json"
    monkeypatch.setattr(settings, "_SETTINGS_FILE", test_settings_file)
    yield


def test_auto_rip_default_false():
    """A fresh install must default auto-rip OFF — silent automatic
    behavior on scan complete would surprise users."""
    assert settings.get_auto_rip_enabled() is False


def test_auto_rip_round_trip_via_setter():
    settings.set_auto_rip_enabled(True)
    assert settings.get_auto_rip_enabled() is True
    settings.set_auto_rip_enabled(False)
    assert settings.get_auto_rip_enabled() is False


def test_setting_survives_other_settings_writes():
    """Saving an unrelated setting must NOT clobber auto_rip_enabled."""
    settings.set_auto_rip_enabled(True)
    settings.save_settings({"eject_on_finish": True})
    assert settings.get_auto_rip_enabled() is True


def test_auto_rip_present_in_known_top_level():
    """Without this membership, save_settings would silently drop the key
    on its allowlisted-keys filter."""
    assert "auto_rip_enabled" in settings._KNOWN_TOP_LEVEL


def test_default_settings_contains_auto_rip():
    """Returns the safe-by-default value at the schema level."""
    defaults = settings._default_settings()
    assert "auto_rip_enabled" in defaults
    assert defaults["auto_rip_enabled"] is False
