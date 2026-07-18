"""Tests for TMDB scraper functionality."""
import pytest
from pathlib import Path
import json
from unittest.mock import patch, MagicMock

from core.tmdb_scraper import (
    scrape_tmdb_page,
    scrape_tmdb_cast_page,
    _parse_runtime_to_minutes,
    parse_tmdb_url,
    normalize_tmdb_id_str,
    normalize_tmdb_type_for_scrape,
    fetch_tmdb_metadata_for_id,
)


def test_parse_runtime_to_minutes():
    """Test runtime parsing helper function."""
    assert _parse_runtime_to_minutes("2h 13m") == 133
    assert _parse_runtime_to_minutes("1h 57m") == 117
    assert _parse_runtime_to_minutes("116m") == 116
    assert _parse_runtime_to_minutes("2h") == 120
    assert _parse_runtime_to_minutes("133m") == 133
    assert _parse_runtime_to_minutes("") is None
    assert _parse_runtime_to_minutes("invalid") is None


def test_parse_tmdb_url():
    """Test TMDB URL parsing."""
    result = parse_tmdb_url("https://www.themoviedb.org/movie/600-full-metal-jacket")
    assert result["type"] == "movie"
    assert result["id"] == "600"
    
    result = parse_tmdb_url("https://www.themoviedb.org/tv/66732-stranger-things")
    assert result["type"] == "tv"
    assert result["id"] == "66732"
    
    with pytest.raises(ValueError):
        parse_tmdb_url("invalid-url")


def test_normalize_tmdb_id_str():
    assert normalize_tmdb_id_str(1893) == "1893"
    assert normalize_tmdb_id_str(" 42 ") == "42"
    assert normalize_tmdb_id_str(None) is None
    assert normalize_tmdb_id_str("") is None


def test_normalize_tmdb_type_for_scrape():
    assert normalize_tmdb_type_for_scrape("Movie") == "movie"
    assert normalize_tmdb_type_for_scrape("TV") == "tv"
    assert normalize_tmdb_type_for_scrape(None, media_type="Movie") == "movie"
    assert normalize_tmdb_type_for_scrape(None, media_type="Series") == "tv"
    assert normalize_tmdb_type_for_scrape(None, group_type="series") == "tv"


def test_fetch_tmdb_metadata_for_id_uses_scrape(monkeypatch):
    monkeypatch.setattr(
        "core.tmdb_scraper.scrape_tmdb_page",
        lambda ttype, tid: {"name": "X", "production_year": 2001, "cover_url": "http://p"},
    )
    out = fetch_tmdb_metadata_for_id("99", "movie")
    assert out == {
        "name": "X",
        "production_year": 2001,
        "cover_url": "http://p",
        "tmdb_type": "movie",
        "tmdb_id": "99",
    }


def test_fetch_tmdb_metadata_for_id_returns_none_on_empty_title(monkeypatch):
    monkeypatch.setattr(
        "core.tmdb_scraper.scrape_tmdb_page",
        lambda ttype, tid: {"name": "", "production_year": None, "cover_url": None},
    )
    assert fetch_tmdb_metadata_for_id("1", "movie") is None


def test_scrape_tmdb_page_tv_series_title_parsing():
    """
    TV series page title is "Show Name (TV Series 2022- ) — ...".
    We must store only the show name and set production_year from the year.
    """
    html = """
    <html>
    <head><title>SAS Rogue Heroes (TV Series 2022- ) — The Movie Database (TMDB)</title></head>
    <body>
    <div class="poster w-full"><img srcset="https://image.tmdb.org/t/p/w500/poster.jpg 1x" /></div>
    </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.text = html
    mock_response.raise_for_status = MagicMock()

    with patch("core.tmdb_scraper.requests.get", return_value=mock_response):
        data = scrape_tmdb_page("tv", "93870")

    assert data["name"] == "SAS Rogue Heroes"
    assert data["production_year"] == 2022


def test_scrape_tmdb_page_tv_series_title_parsing_no_trailing_dash():
    """TV series title can be '(TV Series 2022)' without the trailing '- '."""
    html = """
    <html>
    <head><title>Some Show (TV Series 2019) — The Movie Database (TMDB)</title></head>
    <body>
    <div class="poster w-full"><img src="/poster.jpg" /></div>
    </body>
    </html>
    """
    mock_response = MagicMock()
    mock_response.text = html
    mock_response.raise_for_status = MagicMock()

    with patch("core.tmdb_scraper.requests.get", return_value=mock_response):
        data = scrape_tmdb_page("tv", "12345")

    assert data["name"] == "Some Show"
    assert data["production_year"] == 2019


def test_scrape_tmdb_page_full_metal_jacket(pytestconfig):
    """
    Test scraping the Full Metal Jacket TMDB page.
    Reference: https://www.themoviedb.org/movie/600-full-metal-jacket
    
    This test requires network access. Run with: pytest --run-integration
    """
    if not pytestconfig.getoption("--run-integration", default=False):
        pytest.skip("Skipping integration test (use --run-integration to run)")
    
    data = scrape_tmdb_page("movie", "600")
    
    # Basic fields
    assert data["name"] == "Full Metal Jacket"
    assert data["production_year"] == 1987
    assert data["cover_url"] is not None
    assert data["cover_url"].startswith("http")
    
    # New fields from enhanced scraping
    assert "genres" in data
    assert isinstance(data["genres"], list)
    assert len(data["genres"]) > 0
    assert "Drama" in data["genres"] or "War" in data["genres"]
    
    assert "runtime" in data
    assert data["runtime"] is not None  # Should be something like "1h 57m"
    
    assert "runtime_minutes" in data
    assert data["runtime_minutes"] is not None
    assert isinstance(data["runtime_minutes"], int)
    # Full Metal Jacket is 116 minutes (1h 56m), but TMDB might show 1h 57m
    assert 115 <= data["runtime_minutes"] <= 120
    
    assert "tagline" in data
    assert data["tagline"] is not None
    assert "Vietnam" in data["tagline"] or "care" in data["tagline"].lower()
    
    assert "plot" in data
    assert data["plot"] is not None
    assert "Marine" in data["plot"] or "Vietnam" in data["plot"]
    
    assert "content_rating" in data
    assert data["content_rating"] is not None
    assert data["content_rating"] == "R"
    
    # IMDB ID might not always be available, but check if present
    if data.get("imdb_id"):
        assert data["imdb_id"].startswith("tt")


def test_scrape_tmdb_cast_page_full_metal_jacket(pytestconfig):
    """
    Test scraping the Full Metal Jacket cast page.
    Reference: https://www.themoviedb.org/movie/600-full-metal-jacket/cast
    
    This test requires network access. Run with: pytest --run-integration
    """
    if not pytestconfig.getoption("--run-integration", default=False):
        pytest.skip("Skipping integration test (use --run-integration to run)")
    
    data = scrape_tmdb_cast_page("movie", "600")
    
    assert "directors" in data
    assert isinstance(data["directors"], list)
    assert len(data["directors"]) > 0
    assert "Stanley Kubrick" in data["directors"]
    
    assert "writers" in data
    assert isinstance(data["writers"], list)
    assert len(data["writers"]) > 0
    # Should include at least one of these writers
    writer_names = [w.lower() for w in data["writers"]]
    assert any("kubrick" in w or "herr" in w or "hasford" in w for w in writer_names)
    
    assert "stars" in data
    assert isinstance(data["stars"], list)
    assert len(data["stars"]) > 0
    # Should include at least one of the main cast
    star_names = [s.lower() for s in data["stars"]]
    assert any("modine" in s or "ermey" in s or "d'onofrio" in s for s in star_names)


def test_metadata_json_generation_matches_expected_format(tmp_path, monkeypatch, pytestconfig):
    """
    Test that metadata.json generation matches the expected format from reference file.
    Reference: /tmp/thediscdb/data/movie/Full Metal Jacket (1987)/metadata.json
    
    This test requires network access. Run with: pytest --run-integration
    """
    if not pytestconfig.getoption("--run-integration", default=False):
        pytest.skip("Skipping integration test (use --run-integration to run)")
    
    # Load expected output
    expected_path = Path("/tmp/thediscdb/data/movie/Full Metal Jacket (1987)/metadata.json")
    if not expected_path.exists():
        pytest.skip(f"Expected output file not found: {expected_path}")
    
    with open(expected_path, "r", encoding="utf-8") as f:
        expected = json.load(f)
    
    # Set up test environment
    from core.utils import get_export_root
    monkeypatch.setattr("core.discdb_finalize.get_export_root", lambda: tmp_path / "export")
    
    from core.discdb_finalize import _write_film_metadata
    
    # Create a label_payload with TMDB data
    label_payload = {
        "tmdb_id": "600",
        "tmdb_type": "movie",
        "movie_name": "Full Metal Jacket",
        "production_year": 1987,
        "release_year": 1987,
        "release_slug": "full-metal-jacket-1987",
        "group_type": "movie",
    }
    
    base_dir = tmp_path / "base"
    base_dir.mkdir()
    film_dir = tmp_path / "export" / "movie" / "Full Metal Jacket"
    
    # Write metadata
    _write_film_metadata(base_dir, film_dir, label_payload)
    
    # Read generated metadata
    metadata_path = film_dir / "metadata.json"
    assert metadata_path.exists(), "metadata.json should be created"
    
    with open(metadata_path, "r", encoding="utf-8") as f:
        generated = json.load(f)
    
    # Verify key fields match expected format
    assert generated["Title"] == expected["Title"]
    assert generated["FullTitle"] == expected["FullTitle"]
    assert generated["SortTitle"] == expected["SortTitle"]
    assert generated["Type"] == expected["Type"]
    assert generated["Year"] == expected["Year"]
    assert generated["ExternalIds"]["Tmdb"] == expected["ExternalIds"]["Tmdb"]
    
    # Verify scraped fields are present (may vary slightly due to TMDB page changes)
    if expected.get("Plot"):
        assert "Plot" in generated
        assert len(generated["Plot"]) > 0
    
    if expected.get("Tagline"):
        assert "Tagline" in generated
        assert len(generated["Tagline"]) > 0
    
    if expected.get("Directors"):
        assert "Directors" in generated
        assert "Kubrick" in generated["Directors"]
    
    if expected.get("Writers"):
        assert "Writers" in generated
        # Should contain at least one of the expected writers
        assert any(name in generated["Writers"] for name in ["Kubrick", "Herr", "Hasford"])
    
    if expected.get("Stars"):
        assert "Stars" in generated
        # Should contain at least one of the expected stars
        assert any(name in generated["Stars"] for name in ["Modine", "Ermey", "D'Onofrio"])
    
    if expected.get("Genres"):
        assert "Genres" in generated
        assert len(generated["Genres"]) > 0
    
    if expected.get("RuntimeMinutes"):
        assert "RuntimeMinutes" in generated
        assert isinstance(generated["RuntimeMinutes"], int)
        # Should be close to expected (116 minutes)
        assert 110 <= generated["RuntimeMinutes"] <= 120
    
    if expected.get("ContentRating"):
        assert "ContentRating" in generated
        assert generated["ContentRating"] == "R"
    
    # Verify structure matches
    assert "DateAdded" in generated
    assert "ImageUrl" in generated
    assert "Slug" in generated

