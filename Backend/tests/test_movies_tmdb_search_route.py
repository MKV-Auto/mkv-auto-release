"""Tests for POST /movies/tmdb-search (#387).

The route is the HTTP front for ``core.tmdb_client.search_title``. We mock
the client directly so this layer only verifies request/response shape,
error code mapping, and devmode-toggle behavior.
"""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import tmdb_client


@pytest.fixture(autouse=True)
def _clear_cache():
    tmdb_client.clear_cache()
    yield
    tmdb_client.clear_cache()


@pytest.fixture(autouse=True)
def _bypass_db_readiness_gate(monkeypatch):
    """The route doesn't touch the DB, but FastAPI's readiness middleware fences
    all non-allowlisted paths behind a SELECT 1 against Postgres. In unit tests
    we don't have Postgres; force the gate open.

    Patches the shared ``_ping_db_blocking`` so both the sync façade
    (``_check_db_ready``) and the async middleware path
    (``_check_db_ready_async``) succeed. Also pre-warms the cache state so the
    first request doesn't pay an executor round-trip."""
    import time
    from api import main as api_main
    monkeypatch.setattr(api_main, "_ping_db_blocking", lambda: None)
    monkeypatch.setattr(api_main, "_readiness_state", {
        "checked_at": time.monotonic(),
        "ready": True,
        "error": None,
    })


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_disabled", lambda: False)
    # The route also checks via app_settings (re-imported under a different alias).
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: False)


def test_returns_503_when_key_missing(client, monkeypatch):
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: None)
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: False)
    r = client.post("/movies/tmdb-search", json={"query": "dune"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "tmdb_unavailable"


def test_returns_503_when_devmode_disabled(client, monkeypatch):
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: True)
    r = client.post("/movies/tmdb-search", json={"query": "dune"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "tmdb_unavailable"


def test_returns_empty_candidates_for_blank_query(client, with_key):
    r = client.post("/movies/tmdb-search", json={"query": "   "})
    assert r.status_code == 200
    body = r.json()
    assert body["candidates"] == []
    assert body["normalized_query"] == ""


def test_success_path_returns_normalized_query_and_candidates(client, with_key, monkeypatch):
    """Happy path: the route normalizes, calls the client, returns shaped JSON."""
    def fake_search(query, *, year_hint=None, media_type=None, limit=3):
        return [
            tmdb_client.TmdbCandidate(
                tmdb_id="119051",
                tmdb_type="tv",
                title="Wednesday",
                year=2022,
                cover_url="https://image.tmdb.org/t/p/w500/wed.jpg",
                score=0.91,
            )
        ]

    monkeypatch.setattr(tmdb_client, "search_title", fake_search)
    # The route imports search_title via the module — patch there too.
    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "search_title", fake_search)

    r = client.post(
        "/movies/tmdb-search",
        json={"query": "Wednesday Season 1 Disc 2", "media_type": "tv"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["normalized_query"] == "wednesday"
    assert body["hints"] == {"season": 1, "disc_num": 2}
    assert len(body["candidates"]) == 1
    c = body["candidates"][0]
    assert c["tmdb_id"] == "119051"
    assert c["tmdb_type"] == "tv"
    assert c["title"] == "Wednesday"
    assert c["year"] == 2022


def test_network_error_returns_503_with_distinguishing_code(client, with_key, monkeypatch):
    def raises_network(query, *, year_hint=None, media_type=None, limit=3):
        raise tmdb_client.TmdbNetworkError("upstream timeout")

    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "search_title", raises_network)

    r = client.post("/movies/tmdb-search", json={"query": "anything"})
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "tmdb_network_error"
