"""
Unit tests for core.disc.Disc pure methods (get_movie_data).
No I/O, no load_db_info or rip.
"""
import pytest

from core.disc import Disc


class TestDiscGetMovieData:
    """Tests for Disc.get_movie_data."""

    def test_returns_empty_when_movie_name_falsy(self):
        d = Disc("1", "/mnt/x")
        d.movie_name = None
        assert d.get_movie_data() == {}

        d.movie_name = ""
        assert d.get_movie_data() == {}

    def test_returns_dict_with_name_and_optional_fields(self):
        d = Disc("1", "/mnt/x")
        d.movie_name = "Test Movie"
        d.tmdb_id = "123"
        d.tmdb_type = "movie"
        d.production_year = 2020
        d.original_year = None
        d.release_year = None
        d.release_image = "https://example.com/cover.jpg"
        out = d.get_movie_data()
        assert out == {
            "tmdb_id": "123",
            "tmdb_type": "movie",
            "name": "Test Movie",
            "production_year": 2020,
            "cover_url": "https://example.com/cover.jpg",
        }

    def test_production_year_fallback_to_original_year(self):
        d = Disc("1", "/mnt/x")
        d.movie_name = "X"
        d.production_year = None
        d.original_year = 2018
        d.release_year = 2020
        d.tmdb_id = d.tmdb_type = d.release_image = None
        out = d.get_movie_data()
        assert out["production_year"] == 2018

    def test_production_year_fallback_to_release_year(self):
        d = Disc("1", "/mnt/x")
        d.movie_name = "X"
        d.production_year = None
        d.original_year = None
        d.release_year = 2021
        d.tmdb_id = d.tmdb_type = d.release_image = None
        out = d.get_movie_data()
        assert out["production_year"] == 2021
