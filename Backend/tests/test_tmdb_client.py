"""Tests for tmdb_client.search_title with mocked HTTP (#387).

The real TMDB API is never called. ``requests.get`` is monkeypatched to
return canned JSON for /search/multi, /search/movie, /search/tv. The cache
is cleared between tests so state from one case doesn't pollute another.
"""
from unittest.mock import MagicMock

import pytest

from core import tmdb_client
from core.tmdb_client import (
    TmdbConfigError,
    TmdbNetworkError,
    search_title,
    clear_cache,
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────

def _ok_response(payload):
    resp = MagicMock()
    resp.status_code = 200
    resp.ok = True
    resp.json.return_value = payload
    return resp


def _err_response(status_code, text="error"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = False
    resp.text = text
    return resp


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: "fake-key")


# ──────────────────────────────────────────────────────────────────────────
# Config errors
# ──────────────────────────────────────────────────────────────────────────

def test_search_title_raises_when_key_missing(monkeypatch):
    monkeypatch.setattr(tmdb_client.settings, "get_tmdb_api_key", lambda: None)
    with pytest.raises(TmdbConfigError):
        search_title("dune")


def test_search_title_raises_when_tmdb_returns_401(monkeypatch, with_api_key):
    monkeypatch.setattr(
        tmdb_client.requests, "get",
        lambda *a, **kw: _err_response(401, "invalid_api_key"),
    )
    with pytest.raises(TmdbConfigError):
        search_title("dune")


def test_search_title_raises_network_error_on_500(monkeypatch, with_api_key):
    monkeypatch.setattr(
        tmdb_client.requests, "get",
        lambda *a, **kw: _err_response(500, "internal"),
    )
    with pytest.raises(TmdbNetworkError):
        search_title("dune")


def test_search_title_raises_network_error_on_requests_exception(monkeypatch, with_api_key):
    import requests as _real_requests

    def boom(*a, **kw):
        raise _real_requests.ConnectionError("conn refused")

    monkeypatch.setattr(tmdb_client.requests, "get", boom)
    with pytest.raises(TmdbNetworkError):
        search_title("dune")


# ──────────────────────────────────────────────────────────────────────────
# Happy paths
# ──────────────────────────────────────────────────────────────────────────

def test_search_multi_returns_ranked_candidates(monkeypatch, with_api_key):
    payload = {
        "results": [
            {
                "id": 438631,
                "media_type": "movie",
                "title": "Dune",
                "release_date": "2021-09-15",
                "poster_path": "/dune.jpg",
                "popularity": 45.0,
            },
            {
                "id": 41,
                "media_type": "movie",
                "title": "Dune",
                "release_date": "1984-12-14",
                "poster_path": "/dune_1984.jpg",
                "popularity": 12.0,
            },
            {
                "id": 999,
                "media_type": "person",  # filtered out
                "name": "Dune Person",
            },
        ]
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok_response(payload))

    results = search_title("dune", limit=3)

    assert len(results) == 2  # person filtered out
    # Higher popularity ranks first (same title overlap, no year hint)
    assert results[0].tmdb_id == "438631"
    assert results[0].title == "Dune"
    assert results[0].year == 2021
    assert results[0].cover_url and results[0].cover_url.endswith("/dune.jpg")
    assert 0.0 < results[0].score <= 1.0
    assert results[1].year == 1984


def test_search_title_with_movie_media_type_uses_movie_endpoint(monkeypatch, with_api_key):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _ok_response({
            "results": [
                {"id": 600, "title": "Full Metal Jacket",
                 "release_date": "1987-06-26", "popularity": 30.0, "poster_path": "/fmj.jpg"},
            ]
        })

    monkeypatch.setattr(tmdb_client.requests, "get", fake_get)
    results = search_title("full metal jacket", media_type="movie", limit=1)

    assert "/search/movie" in captured["url"]
    assert captured["params"]["query"] == "full metal jacket"
    assert len(results) == 1
    assert results[0].tmdb_type == "movie"


def test_search_title_with_tv_media_type_uses_tv_endpoint(monkeypatch, with_api_key):
    captured = {}

    def fake_get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        return _ok_response({
            "results": [
                {"id": 119051, "name": "Wednesday",
                 "first_air_date": "2022-11-23", "popularity": 80.0, "poster_path": "/wed.jpg"},
            ]
        })

    monkeypatch.setattr(tmdb_client.requests, "get", fake_get)
    results = search_title("wednesday", media_type="tv", limit=1)

    assert "/search/tv" in captured["url"]
    assert len(results) == 1
    assert results[0].tmdb_type == "tv"
    assert results[0].year == 2022


def test_year_hint_boosts_year_adjacent_result(monkeypatch, with_api_key):
    """Two 'Midway' movies; year_hint=2019 should boost the 2019 over 1976
    despite the older one having higher popularity in this fixture."""
    payload = {
        "results": [
            {
                "id": 11778,
                "media_type": "movie",
                "title": "Midway",
                "release_date": "1976-06-18",
                "popularity": 30.0,  # higher popularity than the remake
                "poster_path": "/midway76.jpg",
            },
            {
                "id": 522162,
                "media_type": "movie",
                "title": "Midway",
                "release_date": "2019-11-08",
                "popularity": 25.0,
                "poster_path": "/midway19.jpg",
            },
        ]
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok_response(payload))

    results = search_title("midway", year_hint=2019)
    assert results[0].tmdb_id == "522162", "year hint should pull the 2019 candidate to the top"


def test_search_title_empty_query_short_circuits(with_api_key):
    """Empty query never hits the network."""
    results = search_title("")
    assert results == []


def test_results_obey_limit(monkeypatch, with_api_key):
    payload = {
        "results": [
            {"id": i, "media_type": "movie", "title": f"Foo {i}",
             "release_date": f"20{i:02d}-01-01", "popularity": float(i), "poster_path": None}
            for i in range(10)
        ]
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok_response(payload))
    results = search_title("foo", limit=3)
    assert len(results) == 3


def test_results_skip_entries_without_title_or_id(monkeypatch, with_api_key):
    payload = {
        "results": [
            {"id": 1, "media_type": "movie"},  # no title
            {"media_type": "movie", "title": "No ID"},  # no id
            {"id": 2, "media_type": "movie", "title": "OK",
             "release_date": "2020-01-01", "popularity": 5.0, "poster_path": None},
        ]
    }
    monkeypatch.setattr(tmdb_client.requests, "get", lambda *a, **kw: _ok_response(payload))
    results = search_title("anything")
    assert len(results) == 1
    assert results[0].tmdb_id == "2"


def test_cache_dedupes_identical_calls(monkeypatch, with_api_key):
    calls = {"n": 0}

    def fake_get(*a, **kw):
        calls["n"] += 1
        return _ok_response({
            "results": [
                {"id": 1, "media_type": "movie", "title": "Cached",
                 "release_date": "2020-01-01", "popularity": 5.0, "poster_path": None},
            ]
        })

    monkeypatch.setattr(tmdb_client.requests, "get", fake_get)

    search_title("cached")
    search_title("cached")
    search_title("cached")

    assert calls["n"] == 1, "search_title should be memoized on its arguments"
