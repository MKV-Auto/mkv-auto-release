"""Unit tests for core.discdb_finalize: _write_film_metadata metadata.json format."""
import json
import pytest
from pathlib import Path


@pytest.fixture(autouse=True)
def _patch_tmdb_scrapers(monkeypatch):
    """Avoid network: stub scrape_tmdb_page and scrape_tmdb_cast_page."""
    monkeypatch.setattr("core.tmdb_scraper.scrape_tmdb_page", lambda *a, **k: {})
    monkeypatch.setattr("core.tmdb_scraper.scrape_tmdb_cast_page", lambda *a, **k: {})


@pytest.fixture
def _patch_dev_mode_off(monkeypatch):
    """Ensure _safe_copy performs real copy for tmdb.json copy-path test."""
    monkeypatch.setattr("core.utils.is_dev_mode", lambda: False)


def test_metadata_json_format_no_tmdb_scrape(tmp_path):
    """metadata.json has Title, FullTitle, SortTitle, Type, Year, ExternalIds.Tmdb from label_payload."""
    from core.discdb_finalize import _write_film_metadata

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    film_dir = tmp_path / "film"
    label_payload = {
        "movie_name": "Test Movie",
        "production_year": 2020,
        "release_name": "Test Release",
        "release_slug": "test-movie-2020",
        "tmdb_id": "12345",
        "tmdb_type": "movie",
        "group_type": "movie",
    }
    _write_film_metadata(base_dir, film_dir, label_payload)

    meta_path = film_dir / "metadata.json"
    assert meta_path.exists()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["Title"] == "Test Movie"
    assert data["FullTitle"] == "Test Movie (2020)"
    assert data["SortTitle"] == "Test Movie"
    assert data["Type"] == "Movie"
    assert data["Year"] == 2020
    assert data["ExternalIds"]["Tmdb"] == "12345"


def test_metadata_json_format_with_preexisting_tmdb_in_base_dir(tmp_path, _patch_dev_mode_off):
    """Pre-existing tmdb.json in base_dir is copied; metadata.json uses it for ExternalIds.Tmdb."""
    from core.discdb_finalize import _write_film_metadata

    base_dir = tmp_path / "base"
    base_dir.mkdir()
    film_dir = tmp_path / "film"
    # Pre-existing tmdb.json to exercise _safe_copy path
    tmdb_src = base_dir / "tmdb.json"
    tmdb_src.write_text(json.dumps({"complete": True, "id": "600", "name": "Full Metal Jacket"}), encoding="utf-8")

    label_payload = {
        "movie_name": "Other Title",
        "production_year": 1987,
        "release_slug": "full-metal-jacket-1987",
        "group_type": "movie",
    }
    _write_film_metadata(base_dir, film_dir, label_payload)

    # Copied tmdb.json exists (copy path exercised)
    tmdb_dest = film_dir / "tmdb.json"
    assert tmdb_dest.exists()
    tmdb_data = json.loads(tmdb_dest.read_text(encoding="utf-8"))
    assert tmdb_data.get("id") == "600"

    meta_path = film_dir / "metadata.json"
    assert meta_path.exists()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert data["Title"] == "Full Metal Jacket"  # from tmdb_data.name
    assert data["Year"] == 1987
    assert data["ExternalIds"]["Tmdb"] == "600"
