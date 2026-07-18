"""Tests for tmdb_client.get_tv_season_episodes (#368)."""
from unittest.mock import MagicMock

import pytest

from core import tmdb_client
from core.tmdb_client import (
    TmdbConfigError,
    TmdbNetworkError,
    TmdbNotFoundError,
    TmdbEpisode,
    TmdbTvDetails,
    get_tv_season_episodes,
    get_tv_details,
    clear_cache,
)


def _ok(payload):
    r = MagicMock()
    r.status_code = 200
    r.ok = True
    r.json.return_value = payload
    return r


def _err(code, text="error"):
    r = MagicMock()
    r.status_code = code
    r.ok = False
    r.text = text
    return r


@pytest.fixture(autouse=True)
def _cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def with_key(monkeypatch):
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: "fake-key")


# ──────────────────────────────────────────────────────────────────────
# Errors
# ──────────────────────────────────────────────────────────────────────

def test_raises_config_error_when_key_missing(monkeypatch):
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: None)
    with pytest.raises(TmdbConfigError):
        get_tv_season_episodes("106379", 2)


def test_raises_config_error_on_401(monkeypatch, with_key):
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _err(401))
    with pytest.raises(TmdbConfigError):
        get_tv_season_episodes("106379", 2)


def test_raises_not_found_on_404(monkeypatch, with_key):
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _err(404, "Season not found"))
    with pytest.raises(TmdbNotFoundError):
        get_tv_season_episodes("106379", 99)


def test_raises_network_error_on_500(monkeypatch, with_key):
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _err(500))
    with pytest.raises(TmdbNetworkError):
        get_tv_season_episodes("106379", 2)


# ──────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────

def test_returns_episodes_for_a_season(monkeypatch, with_key):
    payload = {
        "_id": "ignored",
        "name": "Season 2",
        "season_number": 2,
        "episodes": [
            {
                "season_number": 2, "episode_number": 1,
                "name": "The Cost of Living", "overview": "Lucy returns.",
                "air_date": "2024-12-25", "runtime": 58,
                "still_path": "/abc.jpg",
            },
            {
                "season_number": 2, "episode_number": 2,
                "name": "The Bird Cage", "overview": None,
                "air_date": "2025-01-01", "runtime": None,
                "still_path": None,
            },
        ],
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok(payload))

    out = get_tv_season_episodes("106379", 2)
    assert len(out) == 2
    assert out[0].name == "The Cost of Living"
    assert out[0].episode_number == 1
    assert out[0].runtime == 58
    assert out[0].still_url and out[0].still_url.endswith("/abc.jpg")
    assert out[1].overview is None
    assert out[1].still_url is None


def test_returns_empty_list_when_season_has_no_episodes(monkeypatch, with_key):
    monkeypatch.setattr(
        tmdb_client.requests, "get",
        lambda *a, **kw: _ok({"season_number": 9, "episodes": []}),
    )
    assert get_tv_season_episodes("106379", 9) == []


def test_skips_malformed_episode_entries(monkeypatch, with_key):
    payload = {
        "episodes": [
            {"season_number": 1, "episode_number": 1, "name": "Real"},
            {"name": "missing numbers"},          # skipped
            {"season_number": 1, "episode_number": "n/a", "name": "bad number"},  # skipped
            "not a dict",                          # skipped
            {"season_number": 1, "episode_number": 2, "name": "Real 2"},
        ]
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok(payload))
    out = get_tv_season_episodes("1", 1)
    assert [ep.name for ep in out] == ["Real", "Real 2"]


def test_empty_tmdb_id_returns_empty_list_without_fetch(with_key, monkeypatch):
    """Defensive short-circuit — never POST /tv//season/0."""
    monkeypatch.setattr(
        tmdb_client.requests, "get",
        lambda *a, **kw: pytest.fail("should not call HTTP for blank id"),
    )
    assert get_tv_season_episodes("", 1) == []


def test_cache_dedupes_identical_calls(monkeypatch, with_key):
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return _ok({"episodes": [
            {"season_number": 1, "episode_number": 1, "name": "Pilot"},
        ]})

    monkeypatch.setattr(tmdb_client.requests, "get", counting)
    get_tv_season_episodes("1", 1)
    get_tv_season_episodes("1", 1)
    get_tv_season_episodes("1", 1)
    assert calls["n"] == 1, "LRU cache should dedupe by (tmdb_id, season_number)"


# ──────────────────────────────────────────────────────────────────────
# TV details fold-in (#368)
# ──────────────────────────────────────────────────────────────────────

def test_get_tv_details_returns_number_of_seasons(monkeypatch, with_key):
    payload = {"id": 106379, "name": "Fallout", "number_of_seasons": 2, "status": "Returning Series"}
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok(payload))
    out = get_tv_details("106379")
    assert isinstance(out, TmdbTvDetails)
    assert out.tmdb_id == "106379"
    assert out.name == "Fallout"
    assert out.number_of_seasons == 2
    assert out.status == "Returning Series"


def test_get_tv_details_defaults_to_1_when_missing(monkeypatch, with_key):
    """Defensive: payload without number_of_seasons → 1 (don't break the dropdown)."""
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok({"name": "Mystery Show"}))
    out = get_tv_details("999")
    assert out is not None
    assert out.number_of_seasons == 1
    assert out.status is None


def test_get_tv_details_blank_id_returns_none(with_key, monkeypatch):
    monkeypatch.setattr(
        tmdb_client.requests, "get",
        lambda *a, **kw: pytest.fail("should not call HTTP for blank id"),
    )
    assert get_tv_details("") is None


def test_get_tv_details_cache_dedupes_identical_calls(monkeypatch, with_key):
    calls = {"n": 0}

    def counting(*a, **kw):
        calls["n"] += 1
        return _ok({"name": "X", "number_of_seasons": 3})

    monkeypatch.setattr(tmdb_client.requests, "get", counting)
    get_tv_details("42")
    get_tv_details("42")
    get_tv_details("42")
    assert calls["n"] == 1
