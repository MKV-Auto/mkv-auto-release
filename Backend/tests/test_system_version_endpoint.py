"""GET /system/version (#718) — the running app version for the header."""
from api.routers import system


def test_version_endpoint_reports_baked_version(monkeypatch):
    monkeypatch.setenv("MKVAUTO_VERSION", "1.0.3")
    assert system.get_app_version() == {"version": "1.0.3"}


def test_version_endpoint_falls_back_to_dev(monkeypatch):
    monkeypatch.delenv("MKVAUTO_VERSION", raising=False)
    assert system.get_app_version() == {"version": "dev"}


def test_version_endpoint_trims_and_defaults(monkeypatch):
    monkeypatch.setenv("MKVAUTO_VERSION", "  ")  # whitespace-only → dev
    assert system.get_app_version() == {"version": "dev"}
