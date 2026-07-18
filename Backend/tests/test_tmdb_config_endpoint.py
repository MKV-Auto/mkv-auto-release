"""#610 — Settings → TMDB key should round-trip through the config endpoint.

Pre-#610 the GET response was ``api_key_set: bool`` only — the persisted
key was never echoed. Settings → TMDB field therefore rendered blank
even when configured, while the adjacent MakeMKV section showed its
registration key in clear text. Confusing UX with no actual security
benefit (both keys live on the same disk under the same trust
boundary).

These specs pin the new contract: ``api_key`` is part of the response,
GET returns it for the persisted value, POST echoes it back on save so
the UI doesn't have to re-fetch.
"""
from fastapi.testclient import TestClient

import pytest


@pytest.fixture
def client(test_db, monkeypatch, tmp_path):
    """A TestClient wired to a temp settings.json so each spec gets a
    clean tmdb_api_key starting point."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setenv("MKVAUTO_SETTINGS_PATH", str(settings_path))

    # Re-import settings + the router with the env var in place so
    # load_settings() reads from our tmp path.
    import importlib
    from core import settings as settings_mod
    importlib.reload(settings_mod)

    from api.main import app
    return TestClient(app)


def test_get_returns_persisted_key(client):
    """When a key is on disk, GET /system/tmdb/config returns its value
    under ``api_key`` (not just the boolean)."""
    from core import settings as settings_mod
    settings_mod.set_tmdb_api_key("tmdb-test-key-abc123")

    response = client.get("/system/tmdb/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_set"] is True
    assert body["api_key"] == "tmdb-test-key-abc123"


def test_get_returns_null_when_no_key(client):
    """Empty/unset state — api_key is None, api_key_set is False."""
    from core import settings as settings_mod
    settings_mod.set_tmdb_api_key(None)

    response = client.get("/system/tmdb/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_set"] is False
    assert body["api_key"] is None


def test_post_echoes_saved_key(client):
    """Saving a new key returns the value in the response so the UI can
    update the field without re-fetching GET."""
    response = client.post(
        "/system/tmdb/config",
        json={"api_key": "tmdb-test-key-fresh-save"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_set"] is True
    assert body["api_key"] == "tmdb-test-key-fresh-save"


def test_post_clear_returns_null(client):
    """Clearing the key (empty / null) returns api_key=None so the UI
    field reflects the cleared state."""
    from core import settings as settings_mod
    settings_mod.set_tmdb_api_key("present-then-cleared")

    response = client.post(
        "/system/tmdb/config",
        json={"api_key": None},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["api_key_set"] is False
    assert body["api_key"] is None


def test_roundtrip_post_then_get(client):
    """End-to-end: POST a key, GET reads it back identically."""
    client.post("/system/tmdb/config", json={"api_key": "tmdb-test-key-roundtrip"})
    response = client.get("/system/tmdb/config")
    body = response.json()
    assert body["api_key"] == "tmdb-test-key-roundtrip"
    assert body["api_key_set"] is True
