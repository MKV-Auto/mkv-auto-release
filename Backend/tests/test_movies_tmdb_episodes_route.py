"""Tests for GET /movies/{tmdb_id}/seasons/{season_number}/episodes (#368)."""
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from api.main import app
from core import tmdb_client


@pytest.fixture(autouse=True)
def _clear():
    tmdb_client.clear_cache()
    yield
    tmdb_client.clear_cache()


@pytest.fixture(autouse=True)
def _bypass_db_readiness_gate(monkeypatch):
    """Same pattern as test_movies_tmdb_search_route — patch the shared
    ping no-op and pre-warm the readiness cache so the gate stays open
    without Postgres."""
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


def _with_key(monkeypatch):
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: False)


def test_503_when_key_missing(client, monkeypatch):
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: None)
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: False)
    r = client.get("/movies/106379/seasons/2/episodes")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "tmdb_unavailable"


def test_503_when_devmode_disabled(client, monkeypatch):
    from core import settings as app_settings
    monkeypatch.setattr(app_settings, "get_tmdb_api_key", lambda: "fake-key")
    monkeypatch.setattr(app_settings, "get_tmdb_disabled", lambda: True)
    r = client.get("/movies/106379/seasons/2/episodes")
    assert r.status_code == 503


def test_404_when_tmdb_returns_404(client, monkeypatch):
    _with_key(monkeypatch)

    def raises_not_found(tmdb_id, season_number):
        raise tmdb_client.TmdbNotFoundError("season missing")

    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_season_episodes", raises_not_found)

    r = client.get("/movies/106379/seasons/99/episodes")
    assert r.status_code == 404
    assert r.json()["detail"]["code"] == "tmdb_not_found"


def test_503_on_network_error(client, monkeypatch):
    _with_key(monkeypatch)

    def raises_network(tmdb_id, season_number):
        raise tmdb_client.TmdbNetworkError("connection refused")

    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_season_episodes", raises_network)

    r = client.get("/movies/106379/seasons/2/episodes")
    assert r.status_code == 503
    assert r.json()["detail"]["code"] == "tmdb_network_error"


def test_400_when_season_number_negative(client, monkeypatch):
    _with_key(monkeypatch)
    r = client.get("/movies/106379/seasons/-1/episodes")
    assert r.status_code == 400


def test_happy_path_returns_episodes(client, monkeypatch):
    _with_key(monkeypatch)

    def fake_episodes(tmdb_id, season_number):
        return [
            tmdb_client.TmdbEpisode(
                season_number=2, episode_number=1,
                name="The Cost of Living",
                overview="Lucy returns.",
                air_date="2024-12-25",
                runtime=58,
                still_url="https://image.tmdb.org/t/p/w500/abc.jpg",
            ),
            tmdb_client.TmdbEpisode(
                season_number=2, episode_number=2,
                name="The Bird Cage",
                overview=None, air_date="2025-01-01", runtime=None, still_url=None,
            ),
        ]

    def fake_details(tmdb_id):
        return tmdb_client.TmdbTvDetails(
            tmdb_id=tmdb_id, name="Fallout", number_of_seasons=2, status="Returning Series",
        )

    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_season_episodes", fake_episodes)
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_details", fake_details)

    r = client.get("/movies/106379/seasons/2/episodes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["tmdb_id"] == "106379"
    assert body["season_number"] == 2
    assert len(body["episodes"]) == 2
    e0 = body["episodes"][0]
    assert e0["episode_number"] == 1
    assert e0["name"] == "The Cost of Living"
    assert e0["still_url"].endswith("/abc.jpg")
    assert body["episodes"][1]["overview"] is None
    # tv details fold-in
    assert body["number_of_seasons"] == 2
    assert body["series_name"] == "Fallout"


def test_response_degrades_gracefully_when_tv_details_unavailable(client, monkeypatch):
    """Season payload succeeded but /tv/{id} failed — return episodes with default
    number_of_seasons=1 and series_name=None instead of failing the whole call."""
    _with_key(monkeypatch)

    def fake_episodes(tmdb_id, season_number):
        return [tmdb_client.TmdbEpisode(
            season_number=1, episode_number=1, name="Pilot",
            overview=None, air_date=None, runtime=None, still_url=None,
        )]

    def fake_details(tmdb_id):
        raise tmdb_client.TmdbNetworkError("details endpoint flaky")

    from api.routers import movies as movies_router
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_season_episodes", fake_episodes)
    monkeypatch.setattr(movies_router.tmdb_client, "get_tv_details", fake_details)

    r = client.get("/movies/106379/seasons/1/episodes")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["episodes"]) == 1
    assert body["number_of_seasons"] == 1
    assert body["series_name"] is None
